"""lost_update: read a value, write it back, no decision in between.

The blind overwrite. Two requests both read version A, both write their own
derived value, and one of the two writes silently disappears. There is no
threshold to move into a WHERE clause, so the fix is a row lock or an
optimistic version column rather than an atomic expression.
"""

from __future__ import annotations

from ...contracts import Finding
from .base import (
    RuleContext,
    after,
    base_confidence,
    decision_between,
    guard_verdict,
    linked,
    register,
)


@register("lost_update")
def detect(ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    guard, partial = guard_verdict(ctx)
    if guard:
        return []

    for read in ctx.analysis.reads:
        for write in ctx.analysis.writes:
            if not after(read, write):
                continue
            if ctx.is_claimed(read.line, write.line):
                continue
            if not linked(read, write, ctx.analysis):
                continue
            if write.guards & {"atomic_expression", "with_for_update", "raw_for_update", "version_check"}:
                continue
            decision = decision_between(read, write, ctx.analysis)
            # A threshold decision is oversell's territory, not this rule's.
            if decision is not None and decision.is_threshold:
                continue
            # An existence test followed by an insert is duplicate_insert.
            if decision is not None and decision.is_existence and write.kind == "call":
                if write.site is not None and write.site.construct in {"insert", "orm_add"}:
                    continue

            confidence, why = base_confidence(ctx, read, write, decision, partial)
            findings.append(
                ctx.make_finding(
                    pattern="lost_update",
                    read=read,
                    write=write,
                    decision=decision,
                    confidence=confidence,
                    guard_partial=partial,
                    explanation=(
                        "a value read from the database is written back without "
                        "anything holding the row in between; " + why
                    ),
                )
            )
            break
    return findings
