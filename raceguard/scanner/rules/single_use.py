"""single_use: a token, voucher, invite, or one time action consumed twice.

    coupon = read()
    if coupon.redeemed: reject
    coupon.redeemed = True          # <- both requests saw False

Structurally close to oversell, but the decision is a boolean state check
rather than a threshold, and the invariant is "consumed at most once" rather
than "never below zero".
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

_CONSUMPTION_WORDS = (
    "used",
    "redeem",
    "consumed",
    "claimed",
    "active",
    "revoked",
    "spent",
    "applied",
    "accepted",
    "processed",
    "status",
    "state",
)


@register("single_use")
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
            if decision is None or decision.is_threshold:
                continue
            column = _consumption_column(decision, write)
            if column is None:
                continue

            ctx.claim(read.line, write.line)
            confidence, why = base_confidence(ctx, read, write, decision, partial)
            findings.append(
                ctx.make_finding(
                    pattern="single_use",
                    read=read,
                    write=write,
                    decision=decision,
                    confidence=confidence,
                    guard_partial=partial,
                    columns=[column],
                    explanation=(
                        f"'{column}' is checked and then flipped in the same "
                        f"handler, so two overlapping requests can both consume "
                        f"the same one time value; " + why
                    ),
                )
            )
            break
    return findings


def _consumption_column(decision, write) -> str | None:
    candidates = list(write.columns) + list(decision.columns)
    for name in candidates:
        lowered = name.lower()
        if any(word in lowered for word in _CONSUMPTION_WORDS):
            return name
    return None
