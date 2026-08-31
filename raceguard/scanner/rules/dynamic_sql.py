"""Handlers whose SQL is built at runtime.

Dynamic SQL is the case the tool must not get wrong in either direction. It
cannot be flagged at high confidence, because the scanner genuinely does not
know what the statement touches. It equally cannot be cleared, because a
handler that reads a value, decides on it, and then issues a string-built
UPDATE is the exact defect this tool exists to find, and silence would be a
false negative dressed as restraint.

So it lands at low confidence, with the explanation naming precisely what could
not be resolved. `--min-confidence low` shows it; the default gate does not
fail on it.
"""

from __future__ import annotations

from ...contracts import Confidence, Finding, Pattern
from ..dataflow import Write
from .base import RuleContext, guard_verdict, register


@register("dynamic_sql")
def detect(ctx: RuleContext) -> list[Finding]:
    analysis = ctx.analysis
    if not analysis.unresolved_sites:
        return []
    guard, partial = guard_verdict(ctx)
    if guard:
        return []
    if not analysis.reads or not analysis.decisions:
        return []

    # Only interesting when the unresolved statement runs AFTER a decision that
    # was made on something read from the database. An unresolved statement on
    # its own is just a query the scanner cannot read.
    # Prefer the threshold decision. A handler routinely opens with a
    # `if row is None` existence check before the comparison that actually
    # matters, and taking the first tainted decision mislabels the pattern.
    candidates = [d for d in sorted(analysis.decisions, key=lambda d: d.seq) if d.tainted_names]
    if not candidates:
        return []
    decision = next(
        (d for d in candidates if d.is_threshold),
        next((d for d in candidates if d.is_existence), candidates[0]),
    )
    unresolved = [s for s in analysis.unresolved_sites if s.seq > decision.seq]
    if not unresolved:
        return []

    read = min(
        (r for r in analysis.reads if r.seq < decision.seq),
        key=lambda r: r.seq,
        default=None,
    )
    if read is None:
        return []
    if ctx.is_claimed(read.line, unresolved[0].line):
        return []

    site = unresolved[0]
    write = Write(
        site=site,
        node=site.node,
        line=site.line,
        end_line=site.end_line,
        tainted_names=set(),
        columns=[],
        models=site.models,
        kind="call",
        seq=site.seq,
    )
    pattern: Pattern = (
        "oversell"
        if decision.is_threshold
        else "duplicate_insert"
        if decision.is_existence
        else "lost_update"
    )
    ctx.claim(read.line, write.line)
    return [
        ctx.make_finding(
            pattern=pattern,
            read=read,
            write=write,
            decision=decision,
            confidence=Confidence.low,
            guard_partial=partial,
            explanation=(
                f"a value read from the database is used in a decision and then "
                f"a statement built at runtime is executed at line {site.line}. "
                f"The SQL is constructed by string building, so the table and "
                f"column it writes could not be resolved and this could not be "
                f"confirmed or cleared statically. Reported at low confidence "
                f"deliberately: silence here would be a false negative."
            ),
        )
    ]
