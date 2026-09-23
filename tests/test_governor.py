"""The governor (section 12). RaceGuard is a load generator by construction."""

from __future__ import annotations

import asyncio
import time

import pytest

from raceguard.errors import BudgetExceeded, GovernorRefusal
from raceguard.harness.governor import Governor, TokenBucket


def make(**kwargs) -> Governor:
    defaults = dict(
        max_global_concurrency=4,
        min_trial_spacing_ms=50,
        max_requests_per_run=10,
        refill_per_second=100.0,
        burst_capacity=20.0,
    )
    defaults.update(kwargs)
    return Governor(**defaults)


async def test_request_budget_is_hard():
    governor = make()
    for _ in range(10):
        governor.note_request()
    with pytest.raises(BudgetExceeded):
        governor.note_request()


async def test_inter_trial_spacing_is_enforced():
    """A run should be a series of bursts, not one sustained flood."""
    governor = make(min_trial_spacing_ms=120)
    governor.after_trial()
    started = time.monotonic()
    await governor.before_trial()
    assert (time.monotonic() - started) * 1000 >= 110


async def test_global_ceiling_blocks_beyond_capacity():
    governor = make(max_global_concurrency=2)
    await governor.acquire(2)
    blocked = asyncio.create_task(governor.acquire(1))
    await asyncio.sleep(0.05)
    assert not blocked.done()
    governor.release(2)
    await asyncio.wait_for(blocked, timeout=1)
    governor.release(1)


async def test_token_bucket_bursts_then_throttles():
    """Bursting is the point: a trial is N requests at once. What must stay
    bounded is the sustained rate between trials."""
    bucket = TokenBucket(capacity=5, refill_per_second=1000)
    assert await bucket.take(5) == 0.0
    allowed, delay = bucket.peek(5)
    assert not allowed and delay > 0


def test_reads_both_header_spellings():
    governor = make()
    old = governor.observe(
        {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "30"}
    )
    assert old.limit == 100 and old.remaining == 0 and old.exhausted

    new = governor.observe(
        {"RateLimit": "limit=50, remaining=7, reset=12", "RateLimit-Policy": "50;w=60"}
    )
    assert new.limit == 50 and new.remaining == 7 and new.reset_s == 12
    assert new.policy == "50;w=60"


def test_refuses_when_advertised_limit_is_below_concurrency():
    governor = make()
    snapshot = governor.observe({"RateLimit-Limit": "4"})
    with pytest.raises(GovernorRefusal):
        governor.check_advertised_limit(snapshot, concurrency=8)
    governor.check_advertised_limit(snapshot, concurrency=2)  # fine


async def test_backpressure_stops_the_run():
    """Continuing to send while limited is what gets an integration banned."""
    governor = make()
    await governor.backpressure(429, {"Retry-After": "0"})
    assert governor.stopped_reason and "429" in governor.stopped_reason
    with pytest.raises(GovernorRefusal):
        await governor.acquire(1)
    governor.resume()


def test_ephemeral_targets_are_allowed():
    Governor.check_target_ownership("postgresql://localhost:55432/rg_trial_1", allow_external=False)


def test_external_target_is_refused_by_default():
    with pytest.raises(GovernorRefusal) as excinfo:
        Governor.check_target_ownership("postgresql://staging.internal/app", allow_external=False)
    assert "not an ephemeral container" in excinfo.value.message


def test_external_target_requires_typing_the_host():
    with pytest.raises(GovernorRefusal):
        Governor.check_target_ownership(
            "postgresql://staging.internal/app",
            allow_external=True,
            confirmed_host="wrong.host",
        )
    Governor.check_target_ownership(
        "postgresql://staging.internal/app",
        allow_external=True,
        confirmed_host="staging.internal",
    )


def test_external_target_lowers_the_ceiling():
    governor = Governor(max_global_concurrency=32, external_target=True)
    assert governor.max_global_concurrency == 4


async def test_burst_larger_than_ceiling_does_not_deadlock():
    """A burst wider than the global ceiling is legitimate: the burst is the
    discovery mechanism, and the token bucket, not the semaphore, bounds the
    sustained rate. Capping slots at the ceiling is what keeps acquire(n) with
    n > ceiling from blocking forever on a slot that can never come free."""
    governor = make(max_global_concurrency=4, refill_per_second=1000, burst_capacity=1000)
    slots = await asyncio.wait_for(governor.acquire(30), timeout=2.0)
    assert slots == 4
    governor.release(slots)
    assert governor._semaphore._value == 4  # fully restored, no leak


async def test_concurrent_oversized_bursts_all_complete():
    """Two targets each bursting above the ceiling must not lock-order deadlock
    against each other; every burst completes and the semaphore rebalances."""
    governor = make(max_global_concurrency=8, refill_per_second=1000, burst_capacity=1000)

    async def burst() -> int:
        slots = await governor.acquire(20)
        await asyncio.sleep(0.01)
        governor.release(slots)
        return slots

    got = await asyncio.wait_for(asyncio.gather(*(burst() for _ in range(4))), timeout=5.0)
    assert got == [8, 8, 8, 8]
    assert governor._semaphore._value == 8
