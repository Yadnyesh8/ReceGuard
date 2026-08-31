"""Shared context and registry for the pattern rules.

One module per pattern, each independently testable. A rule receives a fully
built ``RuleContext`` and returns zero or more findings; it never parses,
never touches the filesystem, and never decides suppression.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ...contracts import CodeSpan, Confidence, Finding, Pattern
from ..dataflow import Decision, HandlerAnalysis, Write
from ..matchers import (
    FULL_GUARDS,
    GUARD_SERIALIZABLE,
    Site,
)
from ..routes import HandlerInfo


@dataclass
class RuleContext:
    file: str
    handler: HandlerInfo
    analysis: HandlerAnalysis
    route: str | None
    source_lines: list[str]
    table_of_model: dict[str, str] = field(default_factory=dict)
    unique_keys: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    module_serializable: bool = False
    module_retry_wrapper: bool = False
    # (read line, write line) pairs already explained by a specific pattern.
    # ``lost_update`` is the fallback and must not restate them.
    claimed: set[tuple[int, int]] = field(default_factory=set)

    # -- spans ---------------------------------------------------------

    def span(self, start: int, end: int) -> CodeSpan:
        snippet = ""
        if 1 <= start <= len(self.source_lines):
            end_clamped = min(end, len(self.source_lines))
            snippet = "\n".join(self.source_lines[start - 1 : end_clamped]).strip()
            if len(snippet) > 400:
                snippet = snippet[:397] + "..."
        return CodeSpan(file=self.file, start_line=start, end_line=end, snippet=snippet)

    def tables_for(self, models: list[str]) -> list[str]:
        out: list[str] = []
        for model in models:
            table = self.table_of_model.get(model)
            if table and table not in out:
                out.append(table)
        return out

    # -- guard state ---------------------------------------------------

    @property
    def full_guards(self) -> set[str]:
        return self.analysis.guards & FULL_GUARDS

    @property
    def serializable(self) -> bool:
        return self.module_serializable or GUARD_SERIALIZABLE in self.analysis.guards

    @property
    def retry_wrapper(self) -> bool:
        return self.module_retry_wrapper or self.analysis.has_retry_wrapper

    def claim(self, read_line: int, write_line: int, models: list[str] | None = None) -> None:
        self.claimed.add((read_line, write_line))

    def is_claimed(self, read_line: int, write_line: int) -> bool:
        return (read_line, write_line) in self.claimed

    def make_finding(
        self,
        *,
        pattern: Pattern,
        read: Site,
        write: Write,
        decision: Decision | None,
        confidence: Confidence,
        explanation: str,
        models: list[str] | None = None,
        columns: list[str] | None = None,
        guard_found: str | None = None,
        guard_partial: str | None = None,
    ) -> Finding:
        used_models = models if models is not None else (read.models or write.models)
        tables = self.tables_for(used_models) or [
            m.lower() for m in used_models
        ]
        return Finding(
            id=Finding.make_id(self.file, self.handler.qualname, pattern),
            handler=self.handler.qualname,
            route=self.route,
            pattern=pattern,
            read_site=self.span(read.line, read.end_line),
            decision_site=self.span(decision.line, decision.end_line) if decision else None,
            write_site=self.span(write.line, write.end_line),
            guard_found=guard_found,
            guard_partial=guard_partial,
            confidence=confidence,
            explanation=explanation,
            tables=tables,
            columns=columns or (write.columns or read.columns),
            is_async=self.handler.is_async,
        )


RuleFn = Callable[[RuleContext], list[Finding]]

_REGISTRY: dict[str, RuleFn] = {}


def register(name: str) -> Callable[[RuleFn], RuleFn]:
    def decorator(fn: RuleFn) -> RuleFn:
        if name in _REGISTRY:
            raise RuntimeError(f"duplicate rule registration: {name}")
        _REGISTRY[name] = fn
        return fn

    return decorator


def all_rules() -> dict[str, RuleFn]:
    return dict(_REGISTRY)


# -- shared predicates used by more than one rule ------------------------


def after(read: Site, write: Write) -> bool:
    """Does the write execute after the read?

    Visit order, never file order: a helper defined above the handler holds a
    write whose line number precedes the read it depends on.
    """
    return write.seq > read.seq


def linked(read: Site, write: Write, analysis: HandlerAnalysis) -> bool:
    """Does this write depend on that read?

    Either through taint (the write touches a name bound from the read) or
    through the same model appearing on both ends.
    """
    for name in write.tainted_names:
        taint = analysis.taints.get(name)
        if taint is not None and taint.origin is read:
            return True
    if read.models and write.models:
        return bool(set(read.models) & set(write.models))
    return False


def decision_between(
    read: Site, write: Write, analysis: HandlerAnalysis
) -> Decision | None:
    """The decision on the path: after the read, before the write, same taint."""
    best: Decision | None = None
    for decision in analysis.decisions:
        if not (read.seq < decision.seq < write.seq):
            continue
        for name in decision.tainted_names:
            taint = analysis.taints.get(name)
            if taint is not None and taint.origin is read:
                if best is None or decision.is_threshold:
                    best = decision
                break
    return best


def guard_verdict(ctx: RuleContext) -> tuple[str | None, str | None]:
    """(full guard name, partial guard note). A full guard means: no finding.

    SERIALIZABLE without a retry wrapper is explicitly a PARTIAL guard. It
    converts silent data corruption into user visible 500s, which is better
    but not correct, so it is reported at reduced confidence rather than
    cleared.
    """
    full = ctx.full_guards
    if full:
        return sorted(full)[0], None
    if ctx.serializable:
        if ctx.retry_wrapper:
            return "serializable+retry_wrapper", None
        return None, "serializable isolation, no retry wrapper"
    return None, None


def base_confidence(
    ctx: RuleContext, read: Site, write: Write, decision, partial: str | None = None
) -> tuple[Confidence, str]:
    """Confidence policy from section 6.4, plus the sentence that explains it."""
    confidence, why = _base_confidence(ctx, read, write, decision)
    if partial and confidence is Confidence.high:
        # A partial guard is still something on the path. Reporting it at high
        # would overstate what the scanner actually knows.
        return (
            Confidence.medium,
            f"{why}, but {partial} is present, which converts silent corruption "
            f"into user visible errors without making the sequence atomic",
        )
    return confidence, why


def _base_confidence(ctx: RuleContext, read: Site, write: Write, decision) -> tuple[Confidence, str]:
    if ctx.analysis.dynamic_sql:
        return (
            Confidence.low,
            "SQL is constructed at runtime on this path, so the read and write "
            "targets could not be resolved statically.",
        )
    if ctx.analysis.unresolved:
        return Confidence.low, ctx.analysis.unresolved[0]
    if ctx.analysis.crossed_boundary:
        helpers = ", ".join(ctx.analysis.inlined_helpers)
        return (
            Confidence.medium,
            f"part of this window is inside the helper {helpers}() rather than "
            f"the handler body, so the link crosses a call boundary",
        )
    if write.kind == "helper_call":
        return (
            Confidence.medium,
            "the write happens inside a helper called from this handler, so the "
            "link between the read and the write crosses a call boundary",
        )
    if decision is None:
        return (
            Confidence.medium,
            "the value read here is written back with no decision in between, "
            "which is a blind overwrite of whatever another request stored",
        )
    return (
        Confidence.high,
        "the read, the decision, and the write are all in this handler with "
        "nothing on the path that prevents a second request entering the gap",
    )
