"""GitLab Code Quality report.

GitLab renders ``artifacts:reports:codequality`` inline in the merge request
widget, which is the one place a reviewer looks without opening a pipeline.
The format is the Code Climate subset GitLab documents: a JSON array of
issues, each with a description, a stable fingerprint, a severity, and one
location. Anything outside that subset is ignored, so nothing else is emitted.

Severity mapping: confidence ``high`` is ``major`` rather than ``critical``,
because a finding is a suspicion until verified; a verified violation is
``critical``. Hygiene findings map by their own severity.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..contracts import Confidence, Finding, HygieneFinding, ScanResult, Verdict

_BY_CONFIDENCE = {
    Confidence.high: "major",
    Confidence.medium: "minor",
    Confidence.low: "info",
}
_BY_SEVERITY = {"high": "major", "medium": "minor", "low": "info"}


def build(
    result: ScanResult,
    *,
    verdicts: Iterable[Verdict] = (),
) -> list[dict]:
    violated = {v.finding_id for v in verdicts if v.violated}
    issues: list[dict] = []
    for finding in result.findings:
        issues.append(_issue(finding, verified=finding.id in violated))
    for item in result.hygiene:
        issues.append(_hygiene_issue(item))
    return issues


def _issue(finding: Finding, *, verified: bool) -> dict:
    text = finding.explanation
    if verified:
        text = "VERIFIED: the invariant was violated under concurrent requests. " + text
    return {
        "type": "issue",
        "check_name": f"raceguard/{finding.pattern}",
        "description": text,
        "categories": ["Bug Risk"],
        "severity": "critical" if verified else _BY_CONFIDENCE[finding.confidence],
        "fingerprint": finding.fingerprint,
        "location": {
            "path": finding.read_site.file,
            "lines": {"begin": finding.read_site.start_line},
        },
    }


def _hygiene_issue(item: HygieneFinding) -> dict:
    location = (
        {"path": item.span.file, "lines": {"begin": item.span.start_line}}
        if item.span is not None
        else {"path": "raceguard.yaml", "lines": {"begin": 1}}
    )
    return {
        "type": "issue",
        "check_name": f"raceguard/hygiene/{item.rule}",
        "description": item.explanation,
        "categories": ["Bug Risk"],
        "severity": _BY_SEVERITY.get(item.severity, "info"),
        "fingerprint": item.id,
        "location": location,
    }


def render(result: ScanResult, **kwargs) -> str:
    return json.dumps(build(result, **kwargs), indent=2)
