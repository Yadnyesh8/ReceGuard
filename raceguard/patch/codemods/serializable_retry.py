"""serializable_retry: the loop that makes SERIALIZABLE actually correct.

PostgreSQL's Serializable Snapshot Isolation detects read-write dependency
cycles and aborts one transaction with SQLSTATE 40001. It deliberately provides
no automatic retry, because it cannot know what logic produced the statements,
so the application must re-issue the WHOLE transaction including the decision.

Every loop emitted here is bounded, backs off exponentially with jitter, and
re-raises after the cap. A bare `while True` is never emitted, and an `except`
that catches 40001 and continues without retrying is the bug this fixes, not
the fix.
"""

from __future__ import annotations

import libcst as cst

from .base import StrategyCommand

DESCRIPTION = "Wrap the transaction in a bounded retry for serialization failures."

RETRY_HELPER = '''\
def retry_on_serialization_failure(max_attempts: int = 5, base_delay: float = 0.05):
    """Retry a transaction that PostgreSQL aborted for a serialization failure.

    Bounded, exponential, jittered, and it re-raises once the cap is reached.
    The whole transaction is re-issued, decision logic included: retrying only
    the write would replay a decision made against a snapshot that no longer
    exists.

    40001 serialization_failure, 40P01 deadlock_detected.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await fn(*args, **kwargs)
                except DBAPIError as exc:
                    code = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
                        getattr(exc, "orig", None), "pgcode", None
                    )
                    if code not in ("40001", "40P01"):
                        raise
                    last = exc
                    if attempt == max_attempts - 1:
                        break
                    delay = base_delay * (2**attempt)
                    await asyncio.sleep(delay + random.uniform(0, delay))
            raise last
        return wrapper

    return decorator
'''


class SerializableRetryCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        if self.changed:
            return updated
        self.add_import("asyncio", "")
        self.add_import("functools", "")
        self.add_import("random", "")
        self.add_import("sqlalchemy.exc", "DBAPIError")

        helper = cst.parse_module(RETRY_HELPER)
        body = list(updated.body)
        insert_at = 0
        for index, statement in enumerate(body):
            if isinstance(statement, (cst.Import, cst.ImportFrom)):
                insert_at = index + 1
            elif isinstance(statement, cst.SimpleStatementLine) and any(
                isinstance(s, (cst.Import, cst.ImportFrom)) for s in statement.body
            ):
                insert_at = index + 1
        self.changed = True
        self.notes.append(
            "SERIALIZABLE without a retry wrapper converts silent data corruption "
            "into user visible 500s; the wrapper is what completes the guard"
        )
        return updated.with_changes(
            body=body[:insert_at] + list(helper.body) + body[insert_at:]
        )

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler:
            return super().leave_FunctionDef(original, updated)
        decorator = cst.Decorator(
            decorator=cst.parse_expression("retry_on_serialization_failure()")
        )
        updated = updated.with_changes(decorators=[decorator, *updated.decorators])
        return super().leave_FunctionDef(original, updated)
