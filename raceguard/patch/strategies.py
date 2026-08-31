"""Strategy selection is a table, not a model call.

This is the part a reviewer will probe hardest, so nothing here is a judgement
call made at runtime by a language model. The finding's shape picks the row;
the row picks the transformation. A model only writes the ``rationale`` string
a human reads in the diff, and it has no ability to alter the code emitted.

| Condition                                                    | Strategy            |
|--------------------------------------------------------------|---------------------|
| single row counter, threshold compared then decremented       | atomic_update       |
| single row, decision logic too complex for SQL                | select_for_update   |
| create if absent, natural key identifiable                    | insert_on_conflict  |
| invariant spans rows or tables, no row can carry the lock     | advisory_lock       |
| read handed to a client that later writes back                | optimistic_version  |
| SERIALIZABLE already set but no retry wrapper                 | serializable_retry  |

``atomic_update`` is preferred whenever it fits. It adds no lock, no round
trip, and nothing for a reviewer to object to.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import Finding, Invariant, Strategy


@dataclass
class Decision:
    strategy: Strategy
    reason: str
    needs_migration: bool = False
    confidence: str = "high"


def choose(finding: Finding, invariant: Invariant | None = None) -> Decision:
    kind = invariant.kind if invariant else None

    # SERIALIZABLE without a retry wrapper: the isolation level is already
    # there and the only thing missing is the loop that makes it correct.
    if finding.guard_partial and "serializable" in finding.guard_partial:
        return Decision(
            strategy="serializable_retry",
            reason=(
                "the transaction already runs at SERIALIZABLE, so PostgreSQL will "
                "detect the read-write dependency cycle and abort one side with "
                "SQLSTATE 40001. What is missing is the retry: without it the "
                "abort reaches the user as a 500. PostgreSQL deliberately provides "
                "no automatic retry because it cannot know what logic produced the "
                "statements, so the application must re-issue the whole transaction "
                "including the decision."
            ),
        )

    if finding.pattern == "duplicate_insert" or kind == "duplicate_insert":
        return Decision(
            strategy="insert_on_conflict",
            reason=(
                "this is a create-if-absent flow with no unique constraint, so there "
                "is no atomic guard for the code to lean on. The constraint IS the "
                "fix and the code change is secondary: the migration must ship with "
                "the diff or the code change alone does nothing."
            ),
            needs_migration=True,
        )

    if finding.pattern == "aggregate_limit" or kind == "aggregate_limit":
        return Decision(
            strategy="advisory_lock",
            reason=(
                "the rule spans multiple rows, so no single row can carry the lock. "
                "A transaction scoped advisory lock keyed by the group serialises "
                "just this critical section; the _xact_ variant releases on commit, "
                "which is what you want behind a connection pool."
            ),
        )

    if finding.pattern == "oversell" or kind == "non_negative":
        # The decision has to be expressible in SQL for this to be safe. A
        # decision site that calls out to Python the database cannot evaluate
        # keeps the read where it is and locks the row instead.
        if _decision_fits_in_sql(finding):
            return Decision(
                strategy="atomic_update",
                reason=(
                    "the threshold comparison can move into the WHERE clause, so the "
                    "database evaluates it under its own row lock in a single "
                    "statement. rowcount 0 then means the guard rejected the request. "
                    "This adds no lock, no extra round trip, and nothing for a "
                    "reviewer to object to, which is why it is preferred whenever it "
                    "fits."
                ),
            )
        return Decision(
            strategy="select_for_update",
            reason=(
                "the decision involves logic the database cannot evaluate, so it "
                "cannot move into a WHERE clause. The read takes a row lock instead "
                "and the write stays in the same transaction."
            ),
        )

    if finding.pattern == "single_use" or kind == "single_use":
        return Decision(
            strategy="atomic_update",
            reason=(
                "flipping the consumption flag conditionally in one statement "
                "(WHERE ... AND used = false) makes the check and the write atomic. "
                "rowcount 0 means somebody else consumed it first."
            ),
        )

    if kind == "conservation":
        return Decision(
            strategy="select_for_update",
            reason=(
                "value moves between rows and the total must not change, so both "
                "rows have to be held for the duration. The generated code locks "
                "them in primary key order: locking in inconsistent order across "
                "handlers trades a race for a deadlock."
            ),
        )

    # A blind overwrite with no threshold: nothing to move into a WHERE clause.
    return Decision(
        strategy="optimistic_version",
        reason=(
            "the handler reads a value and writes it back with no decision in "
            "between, so there is no predicate to push into SQL. A version column "
            "makes the write conditional on the row not having changed since the "
            "read; rowcount 0 means somebody else won and the caller gets a 409. "
            "This is the same mechanism the web exposes as ETag plus If-Match."
        ),
        needs_migration=True,
    )


def _decision_fits_in_sql(finding: Finding) -> bool:
    """Can the decision move into a WHERE clause?

    A comparison between a column and a value can. A call into application
    code, a regex, a pricing engine, or anything the database has never heard
    of cannot, and pretending otherwise would generate a patch that changes
    behaviour rather than fixing a race.
    """
    if finding.decision_site is None:
        return False
    snippet = finding.decision_site.snippet
    if not snippet:
        return False
    disqualifying = (
        "await ",
        "requests.",
        "httpx.",
        "json.",
        "re.match",
        "re.search",
        "datetime.now",
        "random.",
    )
    if any(token in snippet for token in disqualifying):
        return False
    # A bare function call inside the test is application logic, not SQL.
    stripped = snippet.split(":", 1)[0]
    if "(" in stripped and not any(
        token in stripped for token in ("len(", "int(", "float(", "Decimal(")
    ):
        return False
    return any(op in snippet for op in ("<", ">", "<=", ">=", "=="))
