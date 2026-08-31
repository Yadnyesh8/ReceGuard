"""Reproduction rate, Wilson interval, and the asymmetric conclusions.

The single most important thing in this module is what it refuses to say.

* One violating trial out of any number is a proof of the defect.
* Zero violating trials out of T is evidence bounded by T, the seed, the
  concurrency, the mode, and the amplification setting. It is not a proof of
  safety.

``reproduction_rate = violating_trials / usable_trials``. The interval is
reported, never asserted on: a gate that fails when a confidence bound moves is
a gate that fails randomly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..contracts import TrialRecord

Z95 = 1.959963984540054


def wilson_interval(successes: int, total: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval. Correct at 0 and at n, unlike the normal one.

    The normal approximation gives (0.0, 0.0) for 0/20, which reads as
    certainty and is exactly the overclaim this tool must never make. Wilson
    gives (0.0, 0.161): "we did not see it, and it could still be up to 16%".
    """
    if total <= 0:
        return (0.0, 0.0)
    successes = max(0, min(successes, total))
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        (p * (1 - p) / total) + (z2 / (4 * total * total))
    )
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


@dataclass
class TrialStats:
    total: int
    usable: int
    violating: int
    errored: int
    reproduction_rate: float
    ci95: tuple[float, float]
    notes: list[str]

    @property
    def violated(self) -> bool:
        return self.violating > 0


def summarise(trials: Sequence[TrialRecord]) -> TrialStats:
    total = len(trials)
    usable_trials = [t for t in trials if t.usable]
    usable = len(usable_trials)
    violating = sum(1 for t in usable_trials if t.violated)
    errored = total - usable
    rate = violating / usable if usable else 0.0

    notes: list[str] = []
    if errored:
        notes.append(
            f"{errored} of {total} trials returned no non-5xx response and were "
            f"excluded from the rate denominator; an invariant that holds because "
            f"the application crashed is not a pass"
        )
    if usable == 0 and total:
        notes.append(
            "no trial produced a usable response, so this run measured nothing"
        )
    return TrialStats(
        total=total,
        usable=usable,
        violating=violating,
        errored=errored,
        reproduction_rate=round(rate, 4),
        ci95=wilson_interval(violating, usable),
        notes=notes,
    )


def conclusion_footer(stats: TrialStats, *, amplified: bool, mode: str, concurrency: int) -> str:
    """The sentence that appears under every verdict. Fixed wording on purpose."""
    if stats.violated:
        return (
            f"The invariant was violated in {stats.violating} of {stats.usable} usable "
            f"trials. One violating trial is a proof of the defect; the rate below it "
            f"({stats.reproduction_rate:.1%}, 95% CI "
            f"{stats.ci95[0]:.1%} to {stats.ci95[1]:.1%}) describes how often it fired "
            f"under these conditions, not how often it will fire in production."
        )
    return (
        f"Not reproduced in {stats.usable} usable trials at concurrency {concurrency} "
        f"({mode} mode{', amplified' if amplified else ', natural timing'}). "
        f"This is evidence bounded by those conditions, not a proof that the handler "
        f"is correct: the true rate is anywhere in 95% CI "
        f"{stats.ci95[0]:.1%} to {stats.ci95[1]:.1%}."
    )


def flake_delta(previous: float, current: float, tolerance: float = 0.25) -> str | None:
    """Nightly corpus drift check: a moved rate is a harness bug, not a code change."""
    if abs(current - previous) <= tolerance:
        return None
    direction = "rose" if current > previous else "fell"
    return (
        f"reproduction rate {direction} from {previous:.1%} to {current:.1%} with no "
        f"code change; investigate the harness before trusting either number"
    )
