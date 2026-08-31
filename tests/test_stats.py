"""Statistics, and the refusal to overclaim (section 8.6)."""

from __future__ import annotations

import pytest

from raceguard.contracts import TrialRecord
from raceguard.harness.stats import (
    conclusion_footer,
    flake_delta,
    summarise,
    wilson_interval,
)


def trial(index: int, *, codes: list[int], violated: bool = False, error=None) -> TrialRecord:
    return TrialRecord(
        index=index, concurrency=2, status_codes=codes, violated=violated, error=error
    )


def test_wilson_never_reports_zero_width_at_zero():
    """0/20 as (0.0, 0.0) reads as certainty. It is the overclaim in numeric
    form and the normal approximation produces it, which is why Wilson is used."""
    low, high = wilson_interval(0, 20)
    assert low == 0.0
    assert 0.1 < high < 0.25


def test_wilson_never_reports_zero_width_at_full():
    low, high = wilson_interval(20, 20)
    assert high == 1.0
    assert low < 1.0


def test_wilson_on_empty_sample():
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_is_bounded():
    for successes in range(0, 11):
        low, high = wilson_interval(successes, 10)
        assert 0.0 <= low <= high <= 1.0


def test_failed_trials_leave_the_denominator():
    """An invariant that held because every request 5xx'd is not a pass."""
    trials = [trial(i, codes=[200, 409], violated=(i < 2)) for i in range(8)]
    trials += [trial(9, codes=[500, 500]), trial(10, codes=[502, 502])]
    stats = summarise(trials)
    assert stats.total == 10
    assert stats.usable == 8
    assert stats.violating == 2
    assert stats.reproduction_rate == pytest.approx(0.25)
    assert any("excluded from the rate denominator" in n for n in stats.notes)


def test_all_trials_unusable_is_called_out():
    stats = summarise([trial(i, codes=[500, 500]) for i in range(5)])
    assert stats.usable == 0
    assert any("measured nothing" in n for n in stats.notes)


def test_footer_calls_one_violation_a_proof():
    stats = summarise([trial(0, codes=[200, 200], violated=True)])
    footer = conclusion_footer(stats, amplified=True, mode="container", concurrency=2)
    assert "proof of the defect" in footer
    assert "not how often it will fire in production" in footer


def test_footer_bounds_a_clean_result():
    stats = summarise([trial(i, codes=[200, 409]) for i in range(20)])
    footer = conclusion_footer(stats, amplified=False, mode="inprocess", concurrency=8)
    assert "Not reproduced" in footer
    assert "not a proof" in footer
    assert "concurrency 8" in footer


def test_flake_detection():
    assert flake_delta(0.9, 0.85) is None
    message = flake_delta(0.9, 0.1)
    assert message and "harness" in message
