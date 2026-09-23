"""Per-client budgets and the process-wide verify gate.

A human runs `raceguard verify` a few times an hour. An agent in a loop will
call `verify_handler` as often as it is allowed to, and every call boots a
database. The budget is keyed by client so one noisy agent cannot exhaust a
shared server for everyone else, and a semaphore bounds how many databases
can be booting at once whoever is asking.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

# Set by the HTTP auth middleware; "stdio" when there is no such thing.
current_client: contextvars.ContextVar[str] = contextvars.ContextVar(
    "raceguard_mcp_client", default="stdio"
)


@dataclass
class Refusal:
    reason: str
    retry_after_s: int | None = None


class Budget:
    def __init__(self, *, session_cap: int, target_cap_per_hour: int, max_concurrent: int) -> None:
        self.session_cap = session_cap
        self.target_cap_per_hour = target_cap_per_hour
        self.max_concurrent = max_concurrent
        self._calls: dict[str, int] = defaultdict(int)
        self._per_target: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        # threading, not asyncio: sync tools run on worker threads.
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._in_flight = 0
        self._closed = False
        self.refusals = 0

    def check(self, client: str, target_id: str) -> Refusal | None:
        with self._lock:
            if self._closed:
                self.refusals += 1
                return Refusal("the server is shutting down and accepts no new verifications")
            if self._calls[client] >= self.session_cap:
                self.refusals += 1
                return Refusal(
                    f"verify_handler is capped at {self.session_cap} calls per client "
                    f"session and that cap is reached. Each call boots an ephemeral "
                    f"PostgreSQL and fires concurrent requests. Use analyze_handler, "
                    f"which is free, or run `raceguard verify` from a terminal."
                )
            now = time.monotonic()
            recent = self._per_target[(client, target_id)]
            while recent and now - recent[0] > 3600:
                recent.popleft()
            if len(recent) >= self.target_cap_per_hour:
                wait = int(3600 - (now - recent[0]))
                self.refusals += 1
                return Refusal(
                    f"target {target_id!r} has been verified {self.target_cap_per_hour} "
                    f"times in the last hour, which is the cap. Re-verifying unchanged "
                    f"code produces the same answer; change the code first.",
                    retry_after_s=wait,
                )
        return None

    def acquire(self) -> bool:
        """A verify slot, or False immediately: never queue an agent invisibly."""
        got = self._slots.acquire(blocking=False)
        if got:
            with self._lock:
                self._in_flight += 1
        return got

    def release(self) -> None:
        with self._lock:
            self._in_flight -= 1
        self._slots.release()

    def record(self, client: str, target_id: str) -> None:
        with self._lock:
            self._calls[client] += 1
            self._per_target[(client, target_id)].append(time.monotonic())

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def drain(self, timeout_s: float) -> bool:
        """Wait for in-flight verifies to finish. True if they all did."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._in_flight == 0:
                return True
            time.sleep(0.1)
        return self._in_flight == 0
