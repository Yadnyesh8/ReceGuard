"""Hygiene checks: idempotency, rate limiting, optimistic concurrency.

Same scanner infrastructure, different rules, aimed one layer up from the SQL.
These findings are cheap to produce and are often the thing a user acts on
first.

The one that genuinely belongs in a race condition tool is
``unsafe_idempotency_check``: code that looks up the key, finds nothing, does
the work, then stores the key is itself a check-then-act race. Two concurrent
replays both miss the lookup and both proceed, which is a duplicate_insert race
in the idempotency layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from ..contracts import CodeSpan, HygieneFinding, HygieneRule
from ..scanner.matchers import dotted, string_literal
from ..scanner.routes import MUTATING_METHODS, collect_routes, compose_route

# Header name is an industry convention popularised by Stripe. The IETF draft
# for it expired without becoming an RFC, so retention windows and parameter
# mismatch behaviour vary by provider. Check for presence and atomic insertion,
# never for conformance to a spec that does not exist.
IDEMPOTENCY_HEADERS = (
    "idempotency-key",
    "idempotency_key",
    "x-idempotency-key",
    "x_idempotency_key",
    "request-id",
    "request_id",
)

# Known Python limiters, plus reverse proxy config that would carry one.
LIMITER_IMPORTS = (
    "slowapi",
    "fastapi_limiter",
    "fastapi_cap",
    "asgi_ratelimit",
    "ratelimit",
    "limits",
    "starlette_limiter",
)
LIMITER_NAME_RE = re.compile(r"(limit|throttl|quota|bucket)", re.IGNORECASE)
PROXY_CONFIG_RE = re.compile(r"(limit_req|limit_conn|ratelimit|rate_limit)", re.IGNORECASE)
PROXY_FILES = ("nginx.conf", "envoy.yaml", "envoy.yml", "traefik.yml", "traefik.yaml")

# FastAPI spells a header parameter with underscores (`if_match: str =
# Header(None)`) and sends `If-Match`. Checking only the hyphenated form misses
# every real implementation.
VALIDATOR_HEADERS = (
    "if-match",
    "if_match",
    "if-unmodified-since",
    "if_unmodified_since",
    "etag",
)
VERSION_COLUMNS = frozenset({"version", "version_id", "etag", "revision", "updated_at", "lock_version"})

RETRYABLE_NAMES = frozenset(
    {"SerializationFailure", "DeadlockDetected", "OperationalError", "TransactionRollbackError"}
)
RETRYABLE_CODES = frozenset({"40001", "40P01"})
BACKOFF_WORDS = frozenset({"sleep", "backoff", "wait", "jitter", "delay"})


@dataclass
class AppFacts:
    """Application wide facts. missing_rate_limit is reported once per app."""

    has_limiter: bool = False
    limiter_evidence: str = ""
    routes_seen: int = 0
    mutating_routes: list[tuple[str, str, int]] = field(default_factory=list)


# --------------------------------------------------------------------------


def check(
    paths: Sequence[str | Path] | str | Path,
    *,
    rules: Iterable[HygieneRule] | None = None,
    root: Path | None = None,
) -> list[HygieneFinding]:
    from ..scanner import iter_python_files

    if isinstance(paths, (str, Path)):
        paths = [paths]
    root = root or Path.cwd()
    enabled = set(rules) if rules is not None else None
    files = iter_python_files(paths, ())

    facts = AppFacts()
    findings: list[HygieneFinding] = []

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _detect_limiter(source):
            facts.has_limiter = True
            facts.limiter_evidence = _relative(path, root)
        if "include_router" not in source and "@" not in source:
            continue
        try:
            module = cst.parse_module(source)
        except cst.ParserSyntaxError:
            continue
        findings.extend(_check_module(module, source, path, root, facts))

    # Reverse proxy config anywhere in the tree also counts as a limiter.
    if not facts.has_limiter:
        for base in {Path(p) for p in paths}:
            for name in PROXY_FILES:
                for candidate in base.rglob(name) if base.is_dir() else []:
                    try:
                        if PROXY_CONFIG_RE.search(candidate.read_text(encoding="utf-8")):
                            facts.has_limiter = True
                            facts.limiter_evidence = _relative(candidate, root)
                    except (OSError, UnicodeDecodeError):
                        continue

    if not facts.has_limiter and facts.routes_seen:
        findings.append(
            HygieneFinding(
                id=HygieneFinding.make_id("<app>", "*", "missing_rate_limit"),
                route="*",
                rule="missing_rate_limit",
                severity="medium",
                explanation=(
                    f"no rate limiter is detectable anywhere in this application "
                    f"({facts.routes_seen} routes found): no limiter middleware, "
                    f"dependency, decorator, or reverse proxy configuration in the "
                    f"repository. Reported once per application rather than once per "
                    f"route."
                ),
                remediation_available=True,
            )
        )

    if enabled is not None:
        findings = [f for f in findings if f.rule in enabled]
    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.route, f.rule))
    return findings


_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _check_module(
    module: cst.Module, source: str, path: Path, root: Path, facts: AppFacts
) -> list[HygieneFinding]:
    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(PositionProvider)
    tree = wrapper.module
    rel = _relative(path, root)
    collector = collect_routes(tree, "")
    lines = source.splitlines()
    out: list[HygieneFinding] = []

    for handler in collector.handlers:
        if not handler.routes:
            continue
        facts.routes_seen += 1
        route = compose_route(handler, collector) or f"{handler.routes[0].method} ?"
        method = handler.routes[0].method
        body_source = cst.Module(body=[handler.node]).code
        span = _span(handler.node, positions, rel, lines)

        if method in MUTATING_METHODS:
            out.extend(_idempotency_rules(handler, body_source, rel, route, span))
            out.extend(_optimistic_rules(handler, method, body_source, rel, route, span))
        out.extend(_retry_rules(handler.node, positions, rel, route, lines))

    return out


# -- 10.1 / 10.2 idempotency ---------------------------------------------


def _idempotency_rules(handler, body: str, file: str, route: str, span) -> list[HygieneFinding]:
    lowered = body.lower()
    reads_key = any(h in lowered for h in IDEMPOTENCY_HEADERS)
    creates = any(
        token in body
        for token in ("session.add", "insert(", ".add_all", "INSERT INTO", "insert into")
    )
    out: list[HygieneFinding] = []

    if not reads_key and creates:
        out.append(
            HygieneFinding(
                id=HygieneFinding.make_id(file, route, "missing_idempotency_key"),
                route=route,
                rule="missing_idempotency_key",
                severity="medium",
                explanation=(
                    "this mutating route creates a record and never reads an "
                    "idempotency header, so a client retry after a timeout creates "
                    "a second one. The Idempotency-Key header is an industry "
                    "convention popularised by Stripe rather than a ratified "
                    "standard; retention window and parameter mismatch behaviour "
                    "vary by provider, so check for presence and atomic insertion, "
                    "not conformance to a spec."
                ),
                remediation_available=False,
                span=span,
            )
        )
        return out

    if reads_key and _lookup_then_act(handler.node):
        out.append(
            HygieneFinding(
                id=HygieneFinding.make_id(file, route, "unsafe_idempotency_check"),
                route=route,
                rule="unsafe_idempotency_check",
                severity="high",
                explanation=(
                    "the idempotency key is looked up, and when the lookup misses "
                    "the work runs and the key is stored afterwards. That is itself "
                    "a check-then-act race: two concurrent replays both miss the "
                    "lookup and both proceed. Claim the key first with a unique "
                    "constraint and treat the constraint violation as 'already in "
                    "flight or done'."
                ),
                remediation_available=True,
                span=span,
            )
        )
    return out


def _lookup_then_act(node: cst.FunctionDef) -> bool:
    """A select on a key table, an existence branch, then an insert of the key.

    The accepted shape inserts first inside a try and catches IntegrityError.
    """
    source = cst.Module(body=[node]).code
    lowered = source.lower()
    if "integrityerror" in lowered and "except" in lowered:
        # Claim-first shape: the insert is guarded by a constraint violation.
        insert_pos = min(
            (lowered.find(t) for t in ("insert(", "session.add") if lowered.find(t) >= 0),
            default=-1,
        )
        except_pos = lowered.find("except")
        if insert_pos >= 0 and except_pos > insert_pos:
            return False
    has_lookup = any(t in lowered for t in ("select(", ".get(", ".scalar(", "query("))
    has_branch = "if " in lowered
    has_store = any(t in lowered for t in ("session.add", "insert("))
    if not (has_lookup and has_branch and has_store):
        return False
    lookup_at = min(
        (lowered.find(t) for t in ("select(", ".get(", ".scalar(", "query(") if lowered.find(t) >= 0),
        default=-1,
    )
    store_at = max(lowered.rfind("session.add"), lowered.rfind("insert("))
    return 0 <= lookup_at < store_at


# -- 10.4 optimistic concurrency ------------------------------------------


def _optimistic_rules(handler, method: str, body: str, file: str, route: str, span):
    if method not in {"PUT", "PATCH"}:
        return []
    lowered = body.lower()
    if any(h in lowered for h in VALIDATOR_HEADERS):
        return []
    if any(col in lowered for col in VERSION_COLUMNS):
        return []
    if not _accepts_full_body(handler.node):
        return []
    if not any(t in lowered for t in ("session.add", "update(", "commit", "=")):
        return []
    return [
        HygieneFinding(
            id=HygieneFinding.make_id(file, route, "missing_optimistic_concurrency"),
            route=route,
            rule="missing_optimistic_concurrency",
            severity="medium",
            explanation=(
                "this route accepts a full resource body and writes it with no "
                "freshness validator: no version column in the where clause, no "
                "If-Match header read, no updated_at equality. That is the classic "
                "lost update at the API layer; the fix is a version or ETag "
                "validator and a 412 when the client's copy is stale."
            ),
            remediation_available=True,
            span=span,
        )
    ]


def _accepts_full_body(node: cst.FunctionDef) -> bool:
    """A parameter annotated with a pydantic-ish model rather than a scalar."""
    for param in node.params.params:
        annotation = param.annotation
        if annotation is None:
            continue
        name = dotted(annotation.annotation)
        tail = name.rsplit(".", 1)[-1]
        if tail and tail[0].isupper() and tail not in {
            "AsyncSession", "Session", "Request", "Response", "UUID", "Decimal",
        }:
            return True
    return False


# -- 10.5 unbounded retry and swallowed serialization failure --------------


def _retry_rules(node: cst.FunctionDef, positions, file: str, route: str, lines):
    out: list[HygieneFinding] = []
    collected: list[tuple[str, cst.CSTNode]] = []

    class _V(cst.CSTVisitor):
        def visit_While(self, loop: cst.While) -> None:
            source = cst.Module(body=[loop]).code
            lowered = source.lower()
            is_forever = isinstance(loop.test, cst.Name) and loop.test.value == "True"
            retries = "except" in lowered and any(
                code.lower() in lowered for code in RETRYABLE_CODES
            ) or bool(RETRYABLE_NAMES & _names(loop))
            if not retries:
                return
            bounded = not is_forever or any(
                t in lowered for t in ("break", "range(", "attempt", "max_retries", "raise")
            )
            has_backoff = bool(BACKOFF_WORDS & {w.lower() for w in _names(loop)})
            if is_forever and not bounded or not has_backoff:
                collected.append(("unbounded_retry", loop))

        def visit_ExceptHandler(self, handler: cst.ExceptHandler) -> None:
            names = _names(handler.type) if handler.type is not None else set()
            codes = _strings(handler)
            if not (names & RETRYABLE_NAMES or codes & RETRYABLE_CODES):
                return
            statements = list(handler.body.body)
            body = cst.Module(body=statements).code.lower()  # type: ignore[arg-type]
            reissues = any(
                t in body for t in ("retry", "continue", "raise", "again", "rollback")
            )
            if not reissues:
                collected.append(("swallowed_serialization_failure", handler))

    node.visit(_V())

    for rule, target in collected:
        span = _span(target, positions, file, lines)
        out.append(
            HygieneFinding(
                id=HygieneFinding.make_id(file, f"{route}:{span.start_line}", rule),
                route=route,
                rule=rule,  # type: ignore[arg-type]
                severity="high" if rule == "swallowed_serialization_failure" else "medium",
                explanation=(
                    "a serialization failure is caught and execution continues "
                    "without re-issuing the transaction, which silently discards "
                    "that transaction's writes. PostgreSQL deliberately provides no "
                    "automatic retry because it cannot know what logic produced the "
                    "statements, so the application must retry the whole transaction "
                    "including the decision logic."
                    if rule == "swallowed_serialization_failure"
                    else "this retry loop is unbounded or has no backoff. Bound it, "
                    "use exponential backoff with jitter, and re-raise after the cap."
                ),
                remediation_available=rule == "unbounded_retry",
                span=span,
            )
        )
    return out


# -- helpers ---------------------------------------------------------------


def _detect_limiter(source: str) -> bool:
    lowered = source.lower()
    if any(f"import {name}" in lowered or f"from {name}" in lowered for name in LIMITER_IMPORTS):
        return True
    for line in source.splitlines():
        stripped = line.strip()
        if not LIMITER_NAME_RE.search(stripped):
            continue
        if stripped.startswith(("class ", "def ", "async def ")) or "add_middleware" in stripped:
            return True
    return False


def _names(node: cst.CSTNode | None) -> set[str]:
    if node is None:
        return set()
    out: set[str] = set()

    class _V(cst.CSTVisitor):
        def visit_Name(self, name: cst.Name) -> None:
            out.add(name.value)

        def visit_Attribute(self, attr: cst.Attribute) -> None:
            out.add(attr.attr.value)

    node.visit(_V())
    return out


def _strings(node: cst.CSTNode) -> set[str]:
    out: set[str] = set()

    class _V(cst.CSTVisitor):
        def visit_SimpleString(self, s: cst.SimpleString) -> None:
            value = string_literal(s)
            if value:
                out.add(value)

    node.visit(_V())
    return out


def _span(node: cst.CSTNode, positions, file: str, lines) -> CodeSpan:
    try:
        pos = positions[node]
        start, end = pos.start.line, pos.end.line
    except Exception:
        start = end = 0
    snippet = ""
    if 1 <= start <= len(lines):
        snippet = "\n".join(lines[start - 1 : min(end, start + 6)]).strip()[:400]
    return CodeSpan(file=file, start_line=start, end_line=end, snippet=snippet)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
