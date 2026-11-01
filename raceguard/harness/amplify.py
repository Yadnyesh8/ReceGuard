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

Implementation: a SQLAlchemy ``after_cursor_execute`` listener on the ``Engine``
class, registered only in the harness process, matching on the target table
name, firing once per request through a context variable. After, not before: a
sleep ahead of the read delays the read itself and leaves the gap between read
and write as narrow as it was. On the class, not an instance, so it also reaches
an engine built lazily or held somewhere no search would find it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import threading
import time
from collections.abc import Iterator
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


def pause(seconds: float) -> None:
    """Hold this request, and only this request, for ``seconds``.

    Under an async engine the listener runs on the event loop, where
    ``time.sleep`` would freeze every other request: the one thing the delay
    exists to let in. SQLAlchemy's bridge yields to the loop instead.
    """
    from sqlalchemy.util import await_only
    from sqlalchemy.util.concurrency import in_greenlet

    try:
        if in_greenlet():
            await_only(asyncio.sleep(seconds))
            return
    except ImportError:
        pass  # no greenlet library installed, so no async engine either
    # A deterministic sleep, not a random one. The point is a window of a known
    # width, so the amplified phase is reproducible rather than merely more
    # likely.
    time.sleep(seconds)


@contextmanager
def amplified(table: str | None, delay_ms: int) -> Iterator[AmplifyState]:
    """Widen the window on every SQLAlchemy engine for the life of the block."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    state = AmplifyState(table=table, delay_ms=delay_ms)

    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if state.enabled and state.delay_ms > 0 and should_delay(statement, state.table):
            _already_amplified.set(True)
            state.note()
            pause(state.delay_ms / 1000.0)

    event.listen(Engine, "after_cursor_execute", _after_cursor_execute)
    try:
        yield state
    finally:
        event.remove(Engine, "after_cursor_execute", _after_cursor_execute)


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
import asyncio
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

    def _pause():
        # Under an async engine this runs on the event loop, where time.sleep
        # would hold back the very request the delay exists to let in.
        try:
            from sqlalchemy.util import await_only
            from sqlalchemy.util.concurrency import in_greenlet

            if in_greenlet():
                await_only(asyncio.sleep(_DELAY_S))
                return
        except ImportError:
            pass
        time.sleep(_DELAY_S)

    @event.listens_for(Engine, "after_cursor_execute")
    def _raceguard_amplify(conn, cursor, statement, parameters, context, executemany):
        if _fired.get() or not _SELECT.match(statement or ""):
            return
        if _TABLE and _TABLE.lower() not in (statement or "").lower():
            return
        _fired.set(True)
        _pause()

    @event.listens_for(Engine, "commit")
    def _raceguard_reset(conn):
        _fired.set(False)
except Exception:
    pass
'''


def sitecustomize_source(table: str | None, delay_ms: int) -> str:
    """The shim to drop into the app container's PYTHONPATH."""
    return SITECUSTOMIZE_TEMPLATE.format(table=table, delay_ms=int(delay_ms))
