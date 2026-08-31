"""The tool must never claim safety.

Section 5 requires a test asserting the string "safe" never appears in report
output, and section 1.3 is explicit about why: RaceGuard says one of three
things and never anything stronger. A tool that overclaims once loses the room
permanently, and a reviewer will look for exactly that overclaim.
"""

from __future__ import annotations

import re

import pytest

from raceguard.contracts import (
    CodeSpan,
    Confidence,
    Finding,
    HygieneFinding,
    Invariant,
    ScanResult,
    TrialRecord,
    Verdict,
)
from raceguard.report import html as html_report
from raceguard.report import json as json_report
from raceguard.report import sarif as sarif_report
from raceguard.report import text as text_report

# Match the CLAIM, not the word. `safe/` as a corpus directory is not a claim,
# "unsafe_idempotency_check" is a rule id, and "is NOT proven correct" is the
# honest sentence this project exists to say, so negated forms are allowed
# through by the lookbehind.
_NEGATION = r"(?<!not )(?<!never )(?<!nor )(?<!n\'t )"
FORBIDDEN = re.compile(
    _NEGATION
    + r"\b(is safe|are safe|secure|verified correct|guaranteed|proven correct|"
    r"proven safe|race[- ]free|bug[- ]free|no race exists)\b",
    re.IGNORECASE,
)


def _span() -> CodeSpan:
    return CodeSpan(file="app/routes.py", start_line=10, end_line=12, snippet="item.stock -= qty")


def _finding() -> Finding:
    return Finding(
        id="abc123",
        handler="app.routes:create_order",
        route="POST /orders",
        pattern="oversell",
        read_site=_span(),
        decision_site=_span(),
        write_site=_span(),
        confidence=Confidence.high,
        explanation="stock is read, compared, and written back with no guard",
        tables=["items"],
    )


def _invariant() -> Invariant:
    return Invariant(
        kind="non_negative",
        table="items",
        column="stock",
        predicate="items.stock >= 0",
        check_sql="SELECT * FROM items WHERE stock < 0",
        source="config",
        derivation="stated in raceguard.yaml",
    )


def _clean_verdict() -> Verdict:
    return Verdict(
        finding_id="abc123",
        target_id="purchase",
        invariant=_invariant(),
        violated=False,
        violating_trials=0,
        total_trials=20,
        usable_trials=20,
        reproduction_rate=0.0,
        ci95=(0.0, 0.161),
        concurrency=2,
        mode="container",
        trials=[TrialRecord(index=i, concurrency=2, status_codes=[200, 409]) for i in range(3)],
        notes=["natural timing: no amplification was applied"],
    )


def _scan() -> ScanResult:
    return ScanResult(
        findings=[_finding()],
        hygiene=[
            HygieneFinding(
                id="h1",
                route="POST /orders",
                rule="missing_idempotency_key",
                severity="medium",
                explanation="no idempotency header is read on this mutating route",
            )
        ],
        files_scanned=12,
    )


RENDERERS = {
    "text.scan": lambda: text_report.render_scan(_scan(), colour=False),
    "text.verdict": lambda: text_report.render_verdict(_clean_verdict(), colour=False),
    "text.two_phase": lambda: text_report.render_two_phase(
        _clean_verdict(), None, colour=False
    ),
    "text.footer": lambda: text_report.render_footer(False),
    "html": lambda: html_report.render(scan=_scan(), verdicts=[_clean_verdict()]),
    "json": lambda: json_report.render(scan=_scan(), verdicts=[_clean_verdict()]),
    "sarif": lambda: sarif_report.render(_scan(), verdicts=[_clean_verdict()]),
}


@pytest.mark.parametrize("name", sorted(RENDERERS))
def test_no_claim_of_safety(name):
    rendered = RENDERERS[name]()
    match = FORBIDDEN.search(rendered)
    assert match is None, (
        f"{name} output claims safety with {match.group(0)!r}. RaceGuard reports "
        f"what it reproduced under stated conditions and never more."
    )


def test_clean_verdict_says_not_reproduced():
    rendered = text_report.render_verdict(_clean_verdict(), colour=False)
    assert "not reproduced" in rendered


def test_verdict_summary_is_conditional():
    summary = _clean_verdict().summary()
    assert "not reproduced" in summary
    assert "concurrency" in summary


def test_zero_violations_still_reports_an_upper_bound():
    """0/20 must never render as an interval of zero width; that reads as
    certainty, which is the exact overclaim this project refuses to make."""
    rendered = text_report.render_verdict(_clean_verdict(), colour=False)
    assert "16.1%" in rendered


def test_footer_states_the_asymmetry():
    footer = text_report.render_footer(False).lower()
    assert "not proven correct" in footer


def test_two_phase_explains_why_natural_was_skipped():
    rendered = text_report.render_two_phase(_clean_verdict(), None, colour=False)
    assert "natural phase skipped" in rendered
