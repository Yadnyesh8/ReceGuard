"""Gap amplification: widening the read-decide-write window on purpose.

Natural reproduction against a local PostgreSQL is unreliable, and an
unreliable tool gets uninstalled. This installs an instrumentation shim that
sleeps a configured number of milliseconds after the FIRST read of the target
table within a request, widening the window deterministically.

Two phases are run per target and BOTH are reported (section 8.4):

* **amplified** proves the read-decide-write window exists and is exploitable.
  That is the structural claim.
* **natural** measures how often it fires with no help. That is the operational
  claim.

They are never merged into one number. The distinction between "your code has a
race window" and "your code loses money three times in a hundred" is the whole
difference between an honest tool and an alarming one.

Implementation: a SQLAlchemy ``before_cursor_execute`` listener registered only
in the harness process, matching on the target table name, firing once per
request through a context variable.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

log = logging.getLogger("raceguard.harness.amplify")

# Set per request so the shim fires once, at the first read, not on every
# statement the handler happens to issue.
_already_amplified: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "raceguard_amplified", default=False
)

_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


@dataclass
class AmplifyState:
    table: str | None
    delay_ms: int
    hits: int = 0
    enabled: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note(self) -> None:
        with self._lock:
            self.hits += 1


def reset_request() -> object:
    """Call at the start of each request so the shim can fire once for it."""
    return _already_amplified.set(False)


def should_delay(statement: str, table: str | None) -> bool:
    """First read of the target table in this request, and nothing else."""
    if _already_amplified.get():
        return False
    if not _SELECT_RE.match(statement or ""):
        return False
    if table and table.lower() not in (statement or "").lower():
        return False
    return True


class Amplifier:
    """Registers and removes the SQLAlchemy event listener."""

    def __init__(self, engine, table: str | None, delay_ms: int) -> None:
        self.engine = engine
        self.state = AmplifyState(table=table, delay_ms=delay_ms)
        self._listener: Callable[..., None] | None = None

    def install(self) -> None:
        try:
            from sqlalchemy import event
        except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency
            return

        state = self.state

        def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if not state.enabled or state.delay_ms <= 0:
                return
            if should_delay(statement, state.table):
                _already_amplified.set(True)
                state.note()
                # A deterministic sleep, not a random one. The point is a
                # window of a known width, so the amplified phase is
                # reproducible rather than merely more likely.
                time.sleep(state.delay_ms / 1000.0)

        target = getattr(self.engine, "sync_engine", self.engine)
        from sqlalchemy import event

        event.listen(target, "before_cursor_execute", _before_cursor_execute)
        self._listener = _before_cursor_execute

    def remove(self) -> None:
        if self._listener is None:
            return
        try:
            from sqlalchemy import event

            target = getattr(self.engine, "sync_engine", self.engine)
            event.remove(target, "before_cursor_execute", self._listener)
        except Exception as exc:  # pragma: no cover - teardown is best effort
            log.debug("could not remove amplifier: %r", exc)
        self._listener = None


@contextmanager
def amplified(engine, table: str | None, delay_ms: int) -> Iterator[AmplifyState]:
    amplifier = Amplifier(engine, table, delay_ms)
    amplifier.install()
    try:
        yield amplifier.state
    finally:
        amplifier.remove()


# --------------------------------------------------------------------------
# Container mode: the shim has to reach the application's own process.
# --------------------------------------------------------------------------

SITECUSTOMIZE_TEMPLATE = '''\
"""Injected by RaceGuard. Widens the read-decide-write window under test.

This file exists only inside an ephemeral verification container. It patches
SQLAlchemy's cursor execution to sleep once per request after the first read of
the table under test, which is what makes reproduction deterministic instead of
dependent on scheduler luck. It is never written into a user's repository.
"""
import os
import re
import time

_TABLE = {table!r}
_DELAY_S = {delay_ms} / 1000.0
_SELECT = re.compile(r"^\\s*(select|with)\\b", re.IGNORECASE)

try:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    import contextvars

    _fired = contextvars.ContextVar("raceguard_amplified", default=False)

    @event.listens_for(Engine, "before_cursor_execute")
    def _raceguard_amplify(conn, cursor, statement, parameters, context, executemany):
        if _fired.get() or not _SELECT.match(statement or ""):
            return
        if _TABLE and _TABLE.lower() not in (statement or "").lower():
            return
        _fired.set(True)
        time.sleep(_DELAY_S)

    @event.listens_for(Engine, "commit")
    def _raceguard_reset(conn):
        _fired.set(False)
except Exception:
    pass
'''


def sitecustomize_source(table: str | None, delay_ms: int) -> str:
    """The shim to drop into the app container's PYTHONPATH."""
    return SITECUSTOMIZE_TEMPLATE.format(table=table, delay_ms=int(delay_ms))
