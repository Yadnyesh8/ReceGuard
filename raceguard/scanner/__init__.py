"""The scanner: five passes per file, bailing out early on files with no routes.

    scan(paths, options) -> ScanResult

Performance budget (section 6.5): a 100 file FastAPI service in under 3 seconds
cold, under 1 second warm with the parse cache. The MCP ``analyze_handler``
tool runs on every file save, so this is a hard requirement rather than a nice
number.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from ..contracts import Confidence, Finding, ScanResult
from .dataflow import analyze_handler
from .matchers import (
    GUARD_RETRY_WRAPPER,
    GUARD_SERIALIZABLE,
    is_isolation_serializable,
)
from .routes import collect_routes, compose_route
from .rules import RuleContext, all_rules
from .suppress import (
    ConfigSuppression,
    InlineSuppression,
    inline_match,
    parse_inline,
)

CACHE_VERSION = "1"
DEFAULT_EXCLUDES = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".raceguard",
    "site-packages",
    "build",
    "dist",
)

# Cheap pre-filter: a file with none of these cannot contain a route handler.
_ROUTE_HINT = re.compile(
    r"@\s*\w+\s*\.\s*(get|post|put|patch|delete|head|options|api_route|route)\b"
)
_TABLENAME_RE = re.compile(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']")


@dataclass
class ScanOptions:
    min_confidence: Confidence = Confidence.medium
    include_hygiene: bool = False
    show_suppressed: bool = False
    config_suppressions: list[ConfigSuppression] = field(default_factory=list)
    cache_dir: Path | None = None
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
    root: Path | None = None
    strict_suppressions: bool = True


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def iter_python_files(paths: Sequence[str | Path], excludes: Iterable[str]) -> list[Path]:
    exclude_set = set(excludes)
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            if path.suffix == ".py" and path.resolve() not in seen:
                seen.add(path.resolve())
                out.append(path)
            continue
        if not path.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in exclude_set and not d.startswith(".")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                candidate = Path(dirpath) / name
                resolved = candidate.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                out.append(candidate)
    return sorted(out)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


class ParseCache:
    """Keyed by file content hash plus tool version. Stores findings, not trees."""

    def __init__(self, directory: Path | None, salt: str = "") -> None:
        self.directory = directory
        self.salt = salt
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def _key(self, source: str, path: Path) -> str:
        digest = hashlib.sha1(
            f"{CACHE_VERSION}|{self.salt}|{path.as_posix()}|{source}".encode()
        ).hexdigest()
        return digest

    def get(self, source: str, path: Path) -> list[Finding] | None:
        if self.directory is None:
            return None
        entry = self.directory / f"{self._key(source, path)}.json"
        if not entry.is_file():
            return None
        try:
            data = json.loads(entry.read_text())
            return [Finding.model_validate(item) for item in data]
        except Exception:
            return None

    def put(self, source: str, path: Path, findings: list[Finding]) -> None:
        if self.directory is None:
            return
        entry = self.directory / f"{self._key(source, path)}.json"
        try:
            entry.write_text(
                json.dumps([f.model_dump(mode="json") for f in findings]), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - cache is best effort
            pass


# --------------------------------------------------------------------------
# Module level facts gathered before the per-handler passes
# --------------------------------------------------------------------------


@dataclass
class ModuleFacts:
    table_of_model: dict[str, str] = field(default_factory=dict)
    unique_keys: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    serializable: bool = False
    retry_wrapper: bool = False
    local_functions: set[str] = field(default_factory=set)
    router_prefixes: dict[str, str] = field(default_factory=dict)
    # Functions whose body catches a serialization failure and re-issues. A
    # retry wrapper is almost always a decorator defined beside the handler
    # rather than a try block inside it, so it has to be found module-wide.
    retry_functions: set[str] = field(default_factory=set)


class ModuleFactVisitor(cst.CSTVisitor):
    """Model to table mapping, unique constraints, engine isolation, helpers."""

    def __init__(self) -> None:
        self.facts = ModuleFacts()
        self._class: str | None = None
        self._depth = 0

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._class = node.name.value
        source = cst.Module(body=[node]).code
        match = _TABLENAME_RE.search(source)
        if match:
            self.facts.table_of_model[node.name.value] = match.group(1)
        else:
            self.facts.table_of_model.setdefault(node.name.value, _snake(node.name.value))
        for unique in _unique_constraints(node):
            table = self.facts.table_of_model[node.name.value]
            self.facts.unique_keys.setdefault(table, []).append(unique)

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._class = None

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if self._depth == 0 and self._class is None:
            self.facts.local_functions.add(node.name.value)
            if _contains_retry(node):
                self.facts.retry_functions.add(node.name.value)
        self._depth += 1
        return True

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth -= 1

    def visit_Call(self, node: cst.Call) -> None:
        if is_isolation_serializable(node):
            self.facts.serializable = True


_RETRY_CODES = ("40001", "40P01")
_RETRY_EXCEPTIONS = (
    "SerializationFailure",
    "DeadlockDetected",
    "OperationalError",
    "TransactionRollbackError",
)


def _contains_retry(node: cst.FunctionDef) -> bool:
    """A try that catches a serialization failure and re-issues the work."""
    source = cst.Module(body=[node]).code
    if "except" not in source:
        return False
    catches = any(name in source for name in _RETRY_EXCEPTIONS) or any(
        code in source for code in _RETRY_CODES
    )
    if not catches:
        return False
    reissues = any(token in source for token in ("continue", "retry", "attempt", "for ", "while "))
    backs_off = True  # bounded-ness is a hygiene finding, not a guard question
    return reissues and backs_off


def _snake(name: str) -> str:
    out = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return out if out.endswith("s") else out + "s"


def _unique_constraints(node: cst.ClassDef) -> list[tuple[str, ...]]:
    """UniqueConstraint(...) and mapped_column(..., unique=True) on a model."""
    found: list[tuple[str, ...]] = []
    source = cst.Module(body=[node])

    class _V(cst.CSTVisitor):
        def visit_Call(self, call: cst.Call) -> None:
            name = call.func.value if isinstance(call.func, cst.Name) else None
            if isinstance(call.func, cst.Attribute):
                name = call.func.attr.value
            if name in {"UniqueConstraint", "Index"}:
                columns: list[str] = []
                unique = name == "UniqueConstraint"
                for arg in call.args:
                    if arg.keyword is not None:
                        if arg.keyword.value == "unique":
                            unique = _is_true(arg.value)
                        continue
                    if isinstance(arg.value, cst.SimpleString):
                        value = arg.value.evaluated_value
                        if isinstance(value, str) and not value.startswith("ix_"):
                            columns.append(value)
                if unique and columns:
                    found.append(tuple(columns))

    source.visit(_V())

    # mapped_column(String, unique=True) attached to a named attribute
    for statement in node.body.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
                if small.value is not None and _has_unique_true(small.value):
                    found.append((small.target.value,))
            elif isinstance(small, cst.Assign):
                for target in small.targets:
                    if isinstance(target.target, cst.Name) and _has_unique_true(small.value):
                        found.append((target.target.value,))
    return found


def _has_unique_true(node: cst.BaseExpression) -> bool:
    if not isinstance(node, cst.Call):
        return False
    for arg in node.args:
        if arg.keyword is not None and arg.keyword.value == "unique" and _is_true(arg.value):
            return True
        if arg.keyword is not None and arg.keyword.value == "primary_key" and _is_true(arg.value):
            return True
    return False


def _is_true(node: cst.BaseExpression) -> bool:
    return isinstance(node, cst.Name) and node.value == "True"


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------


def scan(
    paths: Sequence[str | Path] | str | Path,
    options: ScanOptions | None = None,
) -> ScanResult:
    started = time.perf_counter()
    options = options or ScanOptions()
    if isinstance(paths, (str, Path)):
        paths = [paths]

    root = options.root or Path.cwd()
    cache = ParseCache(options.cache_dir)
    files = iter_python_files(paths, options.excludes)

    findings: list[Finding] = []
    suppressed: list[Finding] = []
    errors: list[str] = []
    external_prefixes = _collect_include_prefixes(files)
    # Models almost never live in the same file as the handlers that use them,
    # so the model -> table map has to be built across the whole scan before
    # any handler is analysed. Without this every table resolves to a guessed
    # snake_case plural and the harness seeds the wrong table.
    global_models = _collect_model_facts(files)
    cache.salt = _facts_digest(global_models)

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue

        # Pass 0: bail out on files with no route decorator at all.
        if not _ROUTE_HINT.search(source):
            continue

        cached = cache.get(source, path)
        if cached is not None:
            file_findings = cached
        else:
            try:
                file_findings = _scan_source(source, path, root, external_prefixes, global_models)
            except cst.ParserSyntaxError as exc:
                errors.append(f"{path}: could not parse: {exc}")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{path}: scanner error: {exc!r}")
                continue
            cache.put(source, path, file_findings)

        inline, problems = parse_inline(source, strict=options.strict_suppressions)
        for problem in problems:
            errors.append(f"{path}: {problem}")

        for finding in file_findings:
            entry = _suppression_for(finding, inline, options.config_suppressions)
            if entry is not None:
                finding.suppressed = True
                finding.suppression_reason = entry
                suppressed.append(finding)
            else:
                findings.append(finding)

    findings = _dedupe(findings)
    findings = [f for f in findings if f.confidence.rank() >= options.min_confidence.rank()]
    findings.sort(key=lambda f: (-f.confidence.rank(), f.read_site.file, f.read_site.start_line))
    suppressed.sort(key=lambda f: (f.read_site.file, f.read_site.start_line))

    return ScanResult(
        findings=findings,
        files_scanned=len(files),
        duration_s=round(time.perf_counter() - started, 4),
        suppressed=suppressed if options.show_suppressed else [],
        errors=errors,
    )


def _scan_source(
    source: str,
    path: Path,
    root: Path,
    external_prefixes: dict[str, str],
    global_models: ModuleFacts | None = None,
) -> list[Finding]:
    module = cst.parse_module(source)
    wrapper = MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(PositionProvider)
    tree = wrapper.module

    module_name = _module_name(path, root)
    collector = collect_routes(tree, module_name)

    fact_visitor = ModuleFactVisitor()
    tree.visit(fact_visitor)
    facts = fact_visitor.facts
    if global_models is not None:
        # File-local definitions win over the global map; a model defined here
        # is the one this handler actually imported.
        merged = dict(global_models.table_of_model)
        merged.update(facts.table_of_model)
        facts.table_of_model = merged
        for table, keys in global_models.unique_keys.items():
            facts.unique_keys.setdefault(table, []).extend(keys)

    helper_bodies = _collect_helper_bodies(tree, {h.name for h in collector.handlers if h.routes})

    source_lines = source.splitlines()
    rel = _relative(path, root)
    rules = all_rules()
    findings: list[Finding] = []

    for handler in collector.handlers:
        if not handler.routes:
            continue
        analysis = analyze_handler(handler.node, positions, facts.local_functions, helper_bodies)
        _promote_helper_writes(analysis, facts)

        if facts.serializable:
            analysis.guards.add(GUARD_SERIALIZABLE)

        # A retry wrapper reaches this handler if the handler wears it, or if
        # any helper the analysis followed wears it, or if the handler itself
        # contains the loop.
        decorated = set(handler.decorators)
        for name in analysis.inlined_helpers:
            helper_node = helper_bodies.get(name)
            if helper_node is not None:
                decorated |= {_decorator_base(d) for d in helper_node.decorators}
        if analysis.has_retry_wrapper or (decorated & facts.retry_functions):
            analysis.has_retry_wrapper = True
            analysis.guards.add(GUARD_RETRY_WRAPPER)

        ctx = RuleContext(
            file=rel,
            handler=handler,
            analysis=analysis,
            route=compose_route(handler, collector, external_prefixes),
            source_lines=source_lines,
            table_of_model=facts.table_of_model,
            unique_keys=facts.unique_keys,
            module_serializable=facts.serializable,
            module_retry_wrapper=analysis.has_retry_wrapper,
        )
        # Order matters: the specific patterns claim their (read, write) pairs
        # and lost_update, the fallback, runs last over what is left.
        fallbacks = {"lost_update": 1, "dynamic_sql": 2}
        ordered = sorted(rules.items(), key=lambda kv: fallbacks.get(kv[0], 0))
        for name, rule in ordered:
            try:
                findings.extend(rule(ctx))
            except Exception as exc:  # pragma: no cover - one rule must not kill a scan
                raise RuntimeError(f"rule {name!r} failed on {rel}: {exc!r}") from exc
    return findings


def _promote_helper_writes(analysis, facts: ModuleFacts) -> None:
    """A read with no in-scope write, plus a call into a local helper, is a
    medium confidence finding with the call site as the write span."""
    if analysis.writes or not analysis.reads or not analysis.helper_calls:
        return
    from .dataflow import Write

    for _name, line in sorted(analysis.helper_calls.items(), key=lambda kv: kv[1]):
        analysis.writes.append(
            Write(
                site=None,
                node=analysis.reads[0].node,
                line=line,
                end_line=line,
                tainted_names=set(analysis.taints),
                columns=[],
                models=analysis.reads[0].models,
                kind="helper_call",
            )
        )
        break


def _decorator_base(dec: cst.Decorator) -> str:
    node = dec.decorator
    if isinstance(node, cst.Call):
        node = node.func
    if isinstance(node, cst.Attribute):
        return node.attr.value
    if isinstance(node, cst.Name):
        return node.value
    return ""


def _collect_helper_bodies(tree: cst.Module, handler_names: set[str]) -> dict[str, cst.FunctionDef]:
    """Module level functions that are not themselves routes."""
    bodies: dict[str, cst.FunctionDef] = {}
    for statement in tree.body:
        if isinstance(statement, cst.FunctionDef) and statement.name.value not in handler_names:
            bodies[statement.name.value] = statement
    return bodies


def _collect_model_facts(files: Sequence[Path]) -> ModuleFacts:
    """One pass over every file for model -> table names and unique keys."""
    facts = ModuleFacts()
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "__tablename__" not in source and "UniqueConstraint" not in source:
            continue
        try:
            module = cst.parse_module(source)
        except cst.ParserSyntaxError:
            continue
        visitor = ModuleFactVisitor()
        module.visit(visitor)
        for model, table in visitor.facts.table_of_model.items():
            facts.table_of_model.setdefault(model, table)
        for table, keys in visitor.facts.unique_keys.items():
            facts.unique_keys.setdefault(table, []).extend(keys)
    return facts


def _facts_digest(facts: ModuleFacts) -> str:
    payload = json.dumps(
        {
            "tables": sorted(facts.table_of_model.items()),
            "unique": sorted((t, sorted(k)) for t, k in facts.unique_keys.items()),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _collect_include_prefixes(files: Sequence[Path]) -> dict[str, str]:
    """``app.include_router(orders.router, prefix="/v1")`` seen anywhere."""
    prefixes: dict[str, str] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "include_router" not in source:
            continue
        try:
            module = cst.parse_module(source)
        except cst.ParserSyntaxError:
            continue
        collector = collect_routes(module, "")
        for include in collector.includes:
            if not include.resolved or not include.prefix:
                continue
            if include.target_module:
                prefixes[f"{include.target_module}.{include.target_name}"] = include.prefix
            prefixes.setdefault(include.target_name, include.prefix)
    return prefixes


def _suppression_for(
    finding: Finding,
    inline: dict[int, InlineSuppression],
    config: list[ConfigSuppression],
) -> str | None:
    # A suppression comment counts anywhere inside the window it is talking
    # about, from the line above the read to the line below the write. People
    # put the comment on the line that reads naturally (often the early return
    # inside the branch), not on whichever line the scanner happens to anchor.
    starts = [finding.read_site.start_line, finding.write_site.start_line]
    ends = [finding.read_site.end_line, finding.write_site.end_line]
    if finding.decision_site:
        starts.append(finding.decision_site.start_line)
        ends.append(finding.decision_site.end_line)
    lines = list(range(min(starts) - 1, max(ends) + 2))

    entry = inline_match(inline, finding.pattern, lines)
    if entry is not None:
        return entry.reason or "(no reason given)"
    for rule in config:
        if rule.matches(rule=finding.pattern, file=finding.read_site.file, handler=finding.handler):
            return rule.reason
    return None


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One finding per physical handler and pattern; keep the highest confidence.

    The read site's line is part of the key, not just the qualname. Two
    handlers in one module can share a name (``async def handler`` on both
    ``/a`` and ``/b``, or a name reused across router files), and keying on the
    name alone silently collapses them into one, dropping a real race on the
    second. The read line pins the finding to a specific handler while still
    collapsing the same handler reported by more than one matcher, which lands
    on the same read.
    """
    best: dict[tuple[str, str, str, int], Finding] = {}
    for finding in findings:
        key = (
            finding.read_site.file,
            finding.handler,
            finding.pattern,
            finding.read_site.start_line,
        )
        current = best.get(key)
        if current is None or finding.confidence.rank() > current.confidence.rank():
            best[key] = finding
    return list(best.values())


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _module_name(path: Path, root: Path) -> str:
    rel = _relative(path, root)
    stem = rel[:-3] if rel.endswith(".py") else rel
    return stem.replace("/", ".").removesuffix(".__init__")
