"""RaceGuard's own concurrency and rate governor.

RaceGuard is a load generator by construction. Left ungoverned it is
indistinguishable from an attack, and the first time someone points it at a
shared staging environment it will take that environment down and the tool will
be banned internally. This module is therefore mandatory, not a setting.

What it enforces (section 12.1):

* a global concurrency ceiling shared across every target,
* a minimum pause between trials, so a run is a series of bursts and not one
  sustained flood,
* a hard per run request budget,
* a token bucket on the outbound side, so sustained rate stays bounded while
  each individual trial still bursts (the burst is the point),
* backpressure on 429 and 503, honouring Retry-After and both spellings of the
  rate limit headers,
* an ownership check, so the thing being hammered is the thing RaceGuard
  created.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..errors import BudgetExceeded, GovernorRefusal

# The older triple remains the widely deployed de facto convention; the newer
# combined fields come from the active IETF httpapi draft. Read both.
_LIMIT_HEADERS = ("ratelimit-limit", "x-ratelimit-limit")
_REMAINING_HEADERS = ("ratelimit-remaining", "x-ratelimit-remaining")
_RESET_HEADERS = ("ratelimit-reset", "x-ratelimit-reset")
_COMBINED_HEADER = "ratelimit"  # e.g. `limit=100, remaining=12, reset=8`
_POLICY_HEADER = "ratelimit-policy"

_COMBINED_RE = re.compile(r"(\w+)\s*=\s*(\d+)")

# A database URL RaceGuard did not create is not one it may hammer.
_EPHEMERAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


@dataclass
class Bucket:
    tokens: float
    last: float


@dataclass
class RateLimitSnapshot:
    limit: int | None = None
    remaining: int | None = None
    reset_s: float | None = None
    retry_after_s: float | None = None
    policy: str | None = None
    source: str = ""

    @property
    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0


@dataclass
class GovernorStats:
    requests_fired: int = 0
    trials_run: int = 0
    pauses: int = 0
    paused_s: float = 0.0
    backpressure_events: int = 0
    observed_limits: list[dict] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requests_fired": self.requests_fired,
            "trials_run": self.trials_run,
            "pauses": self.pauses,
            "paused_s": round(self.paused_s, 3),
            "backpressure_events": self.backpressure_events,
            "observed_limits": self.observed_limits,
            "refusals": self.refusals,
        }


class TokenBucket:
    """Token bucket, the algorithm most public APIs actually use.

    Normal traffic is bursty (batch jobs, app launches, retry storms). A bucket
    accumulates credit while idle and permits a burst up to capacity without
    falsely limiting a well behaved client, which is exactly the shape of a
    trial: a burst of N, then a pause.
    """

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = max(1.0, capacity)
        self.refill_per_second = max(0.1, refill_per_second)
        self._bucket = Bucket(self.capacity, time.monotonic())
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._bucket.last
        self._bucket.tokens = min(
            self.capacity, self._bucket.tokens + elapsed * self.refill_per_second
        )
        self._bucket.last = now

    def peek(self, cost: float = 1.0) -> tuple[bool, float]:
        """(allowed, seconds until allowed). Pure; does not consume."""
        self._refill()
        if self._bucket.tokens >= cost:
            return True, 0.0
        deficit = cost - self._bucket.tokens
        return False, deficit / self.refill_per_second

    async def take(self, cost: float = 1.0) -> float:
        """Consume ``cost`` tokens, waiting if necessary. Returns seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                allowed, delay = self.peek(cost)
                if allowed:
                    self._bucket.tokens -= cost
                    return waited
                await asyncio.sleep(delay)
                waited += delay


class Governor:
    def __init__(
        self,
        *,
        max_global_concurrency: int = 16,
        min_trial_spacing_ms: int = 250,
        max_requests_per_run: int = 5000,
        respect_rate_limit_headers: bool = True,
        refill_per_second: float = 20.0,
        burst_capacity: float = 40.0,
        external_target: bool = False,
        external_concurrency_ceiling: int = 4,
    ) -> None:
        if external_target:
            # Somebody will eventually point this at a URL they believe is
            # staging. Drop the ceiling and say so out loud.
            max_global_concurrency = min(max_global_concurrency, external_concurrency_ceiling)
        self.max_global_concurrency = max_global_concurrency
        self.min_trial_spacing_ms = min_trial_spacing_ms
        self.max_requests_per_run = max_requests_per_run
        self.respect_rate_limit_headers = respect_rate_limit_headers
        self.external_target = external_target

        self._semaphore = asyncio.Semaphore(max_global_concurrency)
        self._bucket = TokenBucket(burst_capacity, refill_per_second)
        self._last_trial_end = 0.0
        self.stats = GovernorStats()
        self.stopped_reason: str | None = None

    # -- ownership ------------------------------------------------------

    @staticmethod
    def check_target_ownership(
        database_url: str, *, allow_external: bool, confirmed_host: str | None = None
    ) -> None:
        """Refuse to run against a database RaceGuard did not create.

        ``--allow-external-target`` requires typing the host name to confirm.
        """
        host = urlparse(database_url).hostname or ""
        if host in _EPHEMERAL_HOSTS:
            return
        if not allow_external:
            raise GovernorRefusal(
                f"refusing to run against {host!r}: the resolved database URL is not "
                f"an ephemeral container RaceGuard created",
                hint=(
                    "pass --allow-external-target and type the host name to confirm. "
                    "Even then the concurrency ceiling drops to 4 and amplification "
                    "is disabled."
                ),
            )
        if confirmed_host != host:
            raise GovernorRefusal(
                f"--allow-external-target requires typing the host name to confirm; "
                f"expected {host!r}, got {confirmed_host!r}"
            )

    # -- budgets --------------------------------------------------------

    def note_request(self, count: int = 1) -> None:
        self.stats.requests_fired += count
        if self.stats.requests_fired > self.max_requests_per_run:
            raise BudgetExceeded(
                f"per run request budget exhausted: {self.stats.requests_fired} requests "
                f"fired, budget is {self.max_requests_per_run}",
                hint="raise governor.max_requests_per_run, or verify fewer targets per run",
            )

    async def acquire(self, n: int = 1) -> None:
        """Take ``n`` slots from the global ceiling and ``n`` tokens."""
        if self.stopped_reason:
            raise GovernorRefusal(f"governor has stopped this run: {self.stopped_reason}")
        for _ in range(n):
            await self._semaphore.acquire()
        waited = await self._bucket.take(float(n))
        if waited:
            self.stats.pauses += 1
            self.stats.paused_s += waited

    def release(self, n: int = 1) -> None:
        for _ in range(n):
            self._semaphore.release()

    async def before_trial(self) -> None:
        """Enforce the inter trial pause so a run is bursts, not a flood."""
        if self._last_trial_end:
            elapsed_ms = (time.monotonic() - self._last_trial_end) * 1000
            remaining = self.min_trial_spacing_ms - elapsed_ms
            if remaining > 0:
                self.stats.pauses += 1
                self.stats.paused_s += remaining / 1000
                await asyncio.sleep(remaining / 1000)

    def after_trial(self) -> None:
        self._last_trial_end = time.monotonic()
        self.stats.trials_run += 1

    # -- backpressure ---------------------------------------------------

    def observe(self, headers) -> RateLimitSnapshot:
        """Read both header spellings off a response."""
        snapshot = RateLimitSnapshot()
        if headers is None:
            return snapshot
        lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}

        for name in _LIMIT_HEADERS:
            if name in lowered:
                snapshot.limit = _int(lowered[name])
                snapshot.source = name
                break
        for name in _REMAINING_HEADERS:
            if name in lowered:
                snapshot.remaining = _int(lowered[name])
                break
        for name in _RESET_HEADERS:
            if name in lowered:
                snapshot.reset_s = _float(lowered[name])
                break
        if _COMBINED_HEADER in lowered:
            parts = dict(_COMBINED_RE.findall(lowered[_COMBINED_HEADER]))
            snapshot.limit = snapshot.limit or _int(parts.get("limit"))
            if snapshot.remaining is None:
                snapshot.remaining = _int(parts.get("remaining"))
            snapshot.reset_s = snapshot.reset_s or _float(parts.get("reset"))
            snapshot.source = snapshot.source or _COMBINED_HEADER
        if _POLICY_HEADER in lowered:
            snapshot.policy = lowered[_POLICY_HEADER]
        if "retry-after" in lowered:
            snapshot.retry_after_s = _float(lowered["retry-after"])

        if snapshot.limit is not None and snapshot.source:
            entry = {
                "limit": snapshot.limit,
                "remaining": snapshot.remaining,
                "reset_s": snapshot.reset_s,
                "policy": snapshot.policy,
                "header": snapshot.source,
            }
            if entry not in self.stats.observed_limits:
                self.stats.observed_limits.append(entry)
        return snapshot

    def check_advertised_limit(self, snapshot: RateLimitSnapshot, concurrency: int) -> None:
        """If the target advertises a limit below our concurrency, do not run."""
        if not self.respect_rate_limit_headers or snapshot.limit is None:
            return
        if snapshot.limit < concurrency:
            raise GovernorRefusal(
                f"target advertises a rate limit of {snapshot.limit} but this target is "
                f"configured to fire {concurrency} concurrent requests",
                hint=(
                    "lower the target's concurrency below the advertised limit, or "
                    "verify against an ephemeral instance instead of this one"
                ),
            )

    async def backpressure(self, status: int, headers, attempt: int = 0) -> float:
        """Stop on 429 or 503. Continuing while limited is what gets bans."""
        if status not in (429, 503):
            return 0.0
        self.stats.backpressure_events += 1
        snapshot = self.observe(headers)
        if snapshot.retry_after_s is not None:
            delay = snapshot.retry_after_s
            reason = "Retry-After"
        elif snapshot.reset_s is not None:
            delay = snapshot.reset_s
            reason = "RateLimit-Reset"
        else:
            delay = min(60.0, (2**attempt)) + random.uniform(0, 0.5)
            reason = "exponential backoff with jitter"
        self.stopped_reason = (
            f"target returned {status}; honouring {reason} and pausing {delay:.1f}s. "
            f"Continuing to send while limited is what gets an integration banned."
        )
        self.stats.refusals.append(self.stopped_reason)
        await asyncio.sleep(min(delay, 60.0))
        return delay

    def resume(self) -> None:
        self.stopped_reason = None


def _int(value) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
