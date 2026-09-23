"""oversell: a counter is compared against a threshold, then decremented.

    stock = read()
    if stock < qty: reject
    stock -= qty            # <- another request already passed the same check

The threshold comparison is what separates this from lost_update, and it is
what makes the fix an atomic UPDATE with the comparison moved into the WHERE
clause rather than a lock.
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


@register("oversell")
def detect(ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    guard, partial = guard_verdict(ctx)
    if guard:
        return []

    for read in ctx.analysis.reads:
        for write in ctx.analysis.writes:
            if not after(read, write):
                continue
            if not linked(read, write, ctx.analysis):
                continue
            if write.guards & {"atomic_expression", "with_for_update", "raw_for_update"}:
                continue
            decision = decision_between(read, write, ctx.analysis)
            if decision is None or not decision.is_threshold:
                continue
            # The write does not have to be a literal `-=`. An
            # `update(...).values(stock=new_stock)` where new_stock was derived
            # from the read is the same defect written a different way, and
            # requiring the decrement spelling missed it entirely.
            if not (
                write.decrement
                or write.kind in {"augassign", "assign_attr", "helper_call"}
                or (write.kind == "call" and _writes_decided_column(decision, write))
            ):
                continue

            ctx.claim(read.line, write.line)
            confidence, why = base_confidence(ctx, read, write, decision, partial)
            column = _column(decision, write)
            findings.append(
                ctx.make_finding(
                    pattern="oversell",
                    read=read,
                    write=write,
                    decision=decision,
                    confidence=confidence,
                    guard_partial=partial,
                    explanation=(
                        f"{column or 'a counter'} is read, compared against a "
                        f"threshold, then written back in the same handler; " + why
                    ),
                    columns=[column] if column else None,
                )
            )
            break
    return findings


def _writes_decided_column(decision, write) -> bool:
    """The column compared in the decision is the column being written."""
    if not write.columns:
        return False
    return bool(set(write.columns) & set(decision.columns))


def _column(decision, write) -> str | None:
    for name in write.columns:
        if name in decision.columns:
            return name
    return write.columns[0] if write.columns else None
