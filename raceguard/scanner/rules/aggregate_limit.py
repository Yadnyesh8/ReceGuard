"""aggregate_limit: a rule spanning multiple rows, checked then extended.

    count = select(count()).where(team_id == t)
    if count >= 5: reject
    session.add(Member(...))        # <- both requests counted 4

No single row can carry the lock, which is why the remediation for this shape
is usually an advisory lock keyed by the group rather than SELECT FOR UPDATE.
"""

from __future__ import annotations

import libcst as cst

from ...contracts import Finding
from ..matchers import dotted
from .base import (
    RuleContext,
    after,
    base_confidence,
    decision_between,
    guard_verdict,
    register,
)

_AGGREGATES = {"count", "sum", "avg", "min", "max", "total"}
_INSERTING = {"insert", "orm_add", "orm_add_all", "raw_write", "upsert"}


@register("aggregate_limit")
def detect(ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    guard, partial = guard_verdict(ctx)
    if guard:
        return []

    for read in ctx.analysis.reads:
        if not _is_aggregate(read):
            continue
        for write in ctx.analysis.writes:
            if not after(read, write):
                continue
            if write.site is None or write.site.construct not in _INSERTING:
                continue
            if write.guards & {"advisory_lock", "with_for_update", "raw_for_update"}:
                continue
            if "advisory_lock" in ctx.analysis.guards:
                continue
            decision = decision_between(read, write, ctx.analysis)
            if decision is None or not decision.is_threshold:
                continue

            ctx.claim(read.line, write.line)
            confidence, why = base_confidence(ctx, read, write, decision, partial)
            findings.append(
                ctx.make_finding(
                    pattern="aggregate_limit",
                    read=read,
                    write=write,
                    decision=decision,
                    confidence=confidence,
                    guard_partial=partial,
                    models=write.models or read.models,
                    explanation=(
                        "a count across rows is compared against a limit and then "
                        "another row is added, so no single row carries the "
                        "decision; " + why
                    ),
                )
            )
            break
    return findings


def _is_aggregate(read) -> bool:
    if read.method in _AGGREGATES:
        return True
    if read.raw_sql and any(f"{fn}(" in read.raw_sql.lower() for fn in _AGGREGATES):
        return True
    node = read.node
    found = False

    class _V(cst.CSTVisitor):
        def visit_Call(self, call: cst.Call) -> None:
            nonlocal found
            name = dotted(call.func).rsplit(".", 1)[-1].lower()
            if name in _AGGREGATES:
                found = True

    if isinstance(node, cst.CSTNode):
        node.visit(_V())
    return found
