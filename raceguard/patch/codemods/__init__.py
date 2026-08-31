"""One libcst transformer per strategy.

Non negotiable behaviours (section 9.3):

* imports go in through ``AddImportsVisitor``, never string editing,
* nothing outside the changed statements is reformatted, because a diff
  touching 200 unrelated lines will not be merged,
* multi row ``select_for_update`` always emits ORDER BY on the primary key.
  Locking rows in inconsistent order across handlers trades a race for a
  deadlock, and shipping that would be worse than shipping nothing,
* every emitted retry loop is bounded, uses exponential backoff with jitter,
  and re-raises after the cap. Never a bare ``while True``.
"""

from .advisory_lock import AdvisoryLockCommand
from .atomic_update import AtomicUpdateCommand
from .insert_on_conflict import InsertOnConflictCommand
from .optimistic_version import OptimisticVersionCommand
from .select_for_update import SelectForUpdateCommand
from .serializable_retry import SerializableRetryCommand

COMMANDS = {
    "atomic_update": AtomicUpdateCommand,
    "select_for_update": SelectForUpdateCommand,
    "insert_on_conflict": InsertOnConflictCommand,
    "advisory_lock": AdvisoryLockCommand,
    "optimistic_version": OptimisticVersionCommand,
    "serializable_retry": SerializableRetryCommand,
}

__all__ = ["COMMANDS", *[c.__name__ for c in COMMANDS.values()]]
