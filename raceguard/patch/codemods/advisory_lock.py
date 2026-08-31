"""advisory_lock: serialise a critical section keyed by the group.

For rules that span rows or tables, where no single row can carry the lock:
"at most five members per team", "at most three active sessions".

The _xact_ variant is deliberate. A transaction scoped lock releases on COMMIT,
which is what you want behind a connection pool; a session scoped one leaks
when the connection is returned still holding it, and the leak only shows up
under load.
"""

from __future__ import annotations

from .base import StrategyCommand

DESCRIPTION = "Serialise a multi-row critical section with a transaction advisory lock."


class AdvisoryLockCommand(StrategyCommand):
    DESCRIPTION = DESCRIPTION

    def leave_FunctionDef(self, original, updated):
        if original.name.value != self.target.handler or self.changed:
            return super().leave_FunctionDef(original, updated)

        located = self.located
        if located.read is None:
            self.bail("could not locate the read to guard")
            return super().leave_FunctionDef(original, updated)

        target = self.target
        if not target.resolved_key:
            self.bail(
                "the value to key the lock on could not be recovered from the "
                "handler, and a lock keyed on an invented name is not a lock"
            )
            return super().leave_FunctionDef(original, updated)
        self.add_import("sqlalchemy", "func")
        self.add_import("sqlalchemy", "select")

        lock = self.parse_statements(
            f"# Transaction scoped advisory lock: released on COMMIT, so it cannot\n"
            f"# leak back into the pool the way a session scoped lock does. Every\n"
            f"# handler touching this invariant must take the SAME key.\n"
            f"{self.await_kw}{target.session}.execute(\n"
            f"    select(func.pg_advisory_xact_lock(func.hashtext({target.lock_key})))\n"
            f")\n"
        )
        self.notes.append(
            f"the lock key is {target.lock_key}; any other handler enforcing this "
            f"same limit must use an identical key or the lock serialises nothing"
        )
        updated = self.splice(updated, lock, start=located.read, end=located.read)
        return super().leave_FunctionDef(original, updated)
