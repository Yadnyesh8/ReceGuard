"""duplicate_insert: create-if-absent with no unique constraint to lean on.

    existing = select(...).where(email == e)
    if existing is None:
        session.add(User(email=e))     # <- two requests both saw None

Without a unique index there is no atomic guard available at all, which is why
the patch for this pattern always carries a migration alongside the code diff.
"""

from __future__ import annotations

from ...contracts import Finding
from .base import (
    RuleContext,
    after,
    base_confidence,
    decision_between,
    guard_verdict,
    register,
)

_INSERTING = {"insert", "orm_add", "orm_add_all", "raw_write", "upsert"}


@register("duplicate_insert")
def detect(ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    guard, partial = guard_verdict(ctx)
    if guard:
        return []

    for read in ctx.analysis.reads:
        for write in ctx.analysis.writes:
            if not after(read, write):
                continue
            if write.site is None or write.site.construct not in _INSERTING:
                continue
            if write.guards & {"on_conflict", "unique_constraint"}:
                continue
            decision = decision_between(read, write, ctx.analysis)
            if decision is None or not decision.is_existence:
                continue

            # An insert whose commit is wrapped in `except IntegrityError` is
            # the claim-first shape: the constraint, not the lookup, decides.
            if ctx.analysis.integrity_guarded:
                return []

            # A reflected unique index on the table being INSERTED INTO is the
            # guard. Checking the table that was read is the wrong direction:
            # the lookup and the insert routinely touch different tables.
            key_columns = set(_key_columns(read, decision)) | set(write.columns)
            for model in write.models or read.models:
                table = ctx.table_of_model.get(model, model.lower())
                uniques = ctx.unique_keys.get(table, [])
                if not uniques:
                    continue
                for unique in uniques:
                    covering = set(unique) - {"id", "pk"}
                    if not covering:
                        continue
                    if covering <= key_columns or covering & key_columns:
                        return []

            ctx.claim(read.line, write.line)
            confidence, why = base_confidence(ctx, read, write, decision, partial)
            findings.append(
                ctx.make_finding(
                    pattern="duplicate_insert",
                    read=read,
                    write=write,
                    decision=decision,
                    confidence=confidence,
                    guard_partial=partial,
                    models=read.models or write.models,
                    columns=sorted(key_columns) or None,
                    explanation=(
                        "the handler checks whether a row exists and inserts one "
                        "when it does not, with no unique constraint making that "
                        "pair atomic; " + why
                    ),
                )
            )
            break
    return findings


def _key_columns(read, decision) -> list[str]:
    columns = [c for c in read.columns if c not in {"id", "pk"}]
    return columns or [c for c in decision.columns if c not in {"id", "pk"}]
