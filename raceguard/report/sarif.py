"""SARIF 2.1.0, the version GitHub code scanning accepts from third party tools.

Constraints worth knowing before reading this file: the uploaded file must be
under 10 MB, private and internal repositories need Code Security enabled, and
if more than one results file is uploaded for the same tool in one workflow
each needs a distinct ``category`` or a unique ``runAutomationDetails.id``.

Mapping (section 14.3):

* ``Finding.pattern`` becomes a reportingDescriptor with a stable id such as
  ``raceguard/oversell``, carrying shortDescription, fullDescription, and a
  help URI, because GitHub surfaces those at the top of an alert.
* ``Finding.confidence`` maps to level: high -> error, medium -> warning,
  low -> note.
* ``read_site`` is the primary location and ``write_site`` a related location,
  so the alert shows both ends of the window.
* verdicts, when present, are appended to the alert message with the
  reproduction rate and the mode.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..contracts import CodeSpan, Confidence, Finding, HygieneFinding, ScanResult, Verdict

SARIF_VERSION = "2.1.0"
SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
DOCS_BASE = "https://github.com/raceguard/raceguard/blob/main/docs/patterns"

_LEVEL = {
    Confidence.high: "error",
    Confidence.medium: "warning",
    Confidence.low: "note",
}
_HYGIENE_LEVEL = {"high": "error", "medium": "warning", "low": "note"}

RULES: dict[str, tuple[str, str]] = {
    "oversell": (
        "Counter checked then decremented without a guard",
        "A counter is compared against a threshold and then written back. Two "
        "concurrent requests can both pass the comparison against the same value "
        "before either commits, so the resource is oversold. Under Read Committed, "
        "the PostgreSQL default, a plain SELECT takes no row lock.",
    ),
    "lost_update": (
        "Value read and written back with nothing holding the row",
        "Two requests both read version A and both write their own derived value, "
        "so one of the two writes disappears silently. There is no threshold to "
        "push into a WHERE clause, so the fix is a row lock or a version column.",
    ),
    "single_use": (
        "One time value checked and consumed without a guard",
        "A token, voucher, or one time action is checked for consumption and then "
        "marked consumed. Both requests can observe the unconsumed state.",
    ),
    "duplicate_insert": (
        "Create-if-absent with no unique constraint",
        "The handler checks whether a row exists and inserts one when it does not. "
        "Without a unique index there is no atomic guard available, so the fix "
        "requires a migration as well as a code change.",
    ),
    "aggregate_limit": (
        "Multi row limit checked then extended",
        "A count across rows is compared against a limit and then another row is "
        "added. No single row can carry the lock, so the remedy is usually an "
        "advisory lock keyed by the group.",
    ),
}

HYGIENE_RULES: dict[str, tuple[str, str]] = {
    "missing_idempotency_key": (
        "Mutating route with no idempotency key",
        "A client retry after a timeout creates a second record. The "
        "Idempotency-Key header is an industry convention popularised by Stripe "
        "rather than a ratified standard.",
    ),
    "unsafe_idempotency_check": (
        "Idempotency key looked up, then acted on",
        "Looking up the key, doing the work, and storing the key afterwards is "
        "itself a check-then-act race: two concurrent replays both miss the lookup. "
        "Claim the key first under a unique constraint.",
    ),
    "missing_rate_limit": (
        "No rate limiter detectable in this application",
        "No limiter middleware, dependency, decorator, or reverse proxy "
        "configuration was found anywhere in the repository.",
    ),
    "missing_optimistic_concurrency": (
        "Full resource write with no freshness validator",
        "The classic lost update at the API layer. The fix is a version or ETag "
        "validator and a 412 when the client's copy is stale.",
    ),
    "unbounded_retry": (
        "Retry loop with no bound or no backoff",
        "Bound the loop, use exponential backoff with jitter, and re-raise after "
        "the cap.",
    ),
    "swallowed_serialization_failure": (
        "Serialization failure caught without retrying",
        "Catching SQLSTATE 40001 and continuing silently discards that "
        "transaction's writes. PostgreSQL provides no automatic retry, so the "
        "application must re-issue the whole transaction.",
    ),
}


def build(
    result: ScanResult,
    *,
    verdicts: Iterable[Verdict] = (),
    tool_version: str = "0.1.0",
    category: str | None = None,
) -> dict:
    verdict_by_finding: dict[str, Verdict] = {}
    for verdict in verdicts:
        if verdict.finding_id:
            # Prefer the amplified one; it is the structural claim.
            existing = verdict_by_finding.get(verdict.finding_id)
            if existing is None or verdict.amplified:
                verdict_by_finding[verdict.finding_id] = verdict

    used_rules = sorted({f.pattern for f in result.findings})
    used_hygiene = sorted({h.rule for h in result.hygiene})

    driver = {
        "name": "RaceGuard",
        "version": tool_version,
        "informationUri": "https://github.com/raceguard/raceguard",
        "rules": [
            _rule(f"raceguard/{name}", *RULES[name], f"{DOCS_BASE}/{name}.md")
            for name in used_rules
            if name in RULES
        ]
        + [
            _rule(f"raceguard/hygiene/{name}", *HYGIENE_RULES[name], f"{DOCS_BASE}/{name}.md")
            for name in used_hygiene
            if name in HYGIENE_RULES
        ],
    }

    run: dict = {
        "tool": {"driver": driver},
        "results": [
            _result(finding, verdict_by_finding.get(finding.id)) for finding in result.findings
        ]
        + [_hygiene_result(item) for item in result.hygiene],
        "columnKind": "utf16CodeUnits",
    }
    if category:
        # Distinct category per uploaded file, so two runs of the same tool in
        # one workflow do not overwrite one another.
        run["automationDetails"] = {"id": f"raceguard/{category}"}

    return {"$schema": SCHEMA, "version": SARIF_VERSION, "runs": [run]}


def _rule(rule_id: str, short: str, full: str, help_uri: str) -> dict:
    return {
        "id": rule_id,
        "name": rule_id.split("/")[-1],
        "shortDescription": {"text": short},
        "fullDescription": {"text": full},
        "helpUri": help_uri,
        "help": {"text": full, "markdown": f"**{short}**\n\n{full}\n\n[Docs]({help_uri})"},
        "defaultConfiguration": {"level": "warning"},
    }


def _location(span: CodeSpan, message: str | None = None) -> dict:
    location = {
        "physicalLocation": {
            "artifactLocation": {"uri": span.file, "uriBaseId": "%SRCROOT%"},
            "region": {
                "startLine": max(1, span.start_line),
                "endLine": max(span.start_line, span.end_line),
            },
        }
    }
    if message:
        location["message"] = {"text": message}
    return location


def _result(finding: Finding, verdict: Verdict | None) -> dict:
    text = finding.explanation
    if finding.guard_partial:
        text += f" A partial guard is present: {finding.guard_partial}."
    if verdict is not None:
        if verdict.violated:
            text += (
                f" Verified dynamically: the invariant was violated in "
                f"{verdict.violating_trials} of {verdict.usable_trials} usable trials "
                f"({verdict.reproduction_rate:.0%}) at concurrency {verdict.concurrency} "
                f"in {verdict.mode} mode"
                f"{', amplified' if verdict.amplified else ', natural timing'}."
            )
        else:
            text += (
                f" Verification ran and did NOT reproduce a violation in "
                f"{verdict.usable_trials} trials under those conditions. That bounds "
                f"the evidence; it does not establish correctness."
            )

    return {
        "ruleId": f"raceguard/{finding.pattern}",
        "level": _LEVEL.get(finding.confidence, "note"),
        "message": {"text": text},
        "locations": [_location(finding.read_site, "the read")],
        "relatedLocations": [
            location
            for location in (
                _location(finding.write_site, "the write that closes the window"),
                _location(finding.decision_site, "the decision") if finding.decision_site else None,
            )
            if location is not None
        ],
        "partialFingerprints": {"raceguardFindingId": finding.id},
        "properties": {
            "confidence": finding.confidence.value,
            "handler": finding.handler,
            "route": finding.route,
            "tables": finding.tables,
        },
    }


def _hygiene_result(item: HygieneFinding) -> dict:
    result: dict = {
        "ruleId": f"raceguard/hygiene/{item.rule}",
        "level": _HYGIENE_LEVEL.get(item.severity, "note"),
        "message": {"text": item.explanation},
        "partialFingerprints": {"raceguardFindingId": item.id},
        "properties": {"route": item.route, "severity": item.severity},
    }
    result["locations"] = [_location(item.span)] if item.span else [
        {
            "physicalLocation": {
                "artifactLocation": {"uri": "raceguard.yaml", "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": 1},
            }
        }
    ]
    return result


def render(result: ScanResult, **kwargs) -> str:
    return json.dumps(build(result, **kwargs), indent=2)
