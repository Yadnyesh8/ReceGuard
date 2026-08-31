"""Inline and config suppression.

    item.stock -= qty  # raceguard: ignore[oversell] guarded by upstream advisory lock

The comment must name a rule id and a free text reason after the bracket is
required, enforced here rather than documented and hoped for. A suppression
without a reason is a silent decision, and silent decisions are what make a
suppression file worthless a year later.

`raceguard scan --show-suppressed` lists everything suppressed with its reason,
so suppressions stay auditable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

SUPPRESS_RE = re.compile(
    r"#\s*raceguard:\s*ignore\[(?P<rules>[^\]]*)\]\s*(?P<reason>.*)$", re.IGNORECASE
)

WILDCARD = "*"


class SuppressionError(ValueError):
    """A suppression comment that is present but unusable."""


@dataclass
class InlineSuppression:
    line: int
    rules: frozenset[str]
    reason: str
    raw: str

    def covers(self, rule: str) -> bool:
        return WILDCARD in self.rules or rule in self.rules


def parse_inline(source: str, *, strict: bool = True) -> tuple[dict[int, InlineSuppression], list[str]]:
    """Scan raw source text for suppression comments. Returns (by line, problems)."""
    found: dict[int, InlineSuppression] = {}
    problems: list[str] = []
    for number, text in enumerate(source.splitlines(), start=1):
        match = SUPPRESS_RE.search(text)
        if match is None:
            continue
        rules = frozenset(
            part.strip() for part in match.group("rules").split(",") if part.strip()
        )
        reason = match.group("reason").strip()
        if not rules:
            problems.append(
                f"line {number}: raceguard: ignore[...] names no rule; "
                f"use ignore[oversell] or ignore[*]"
            )
            continue
        if not reason:
            problems.append(
                f"line {number}: raceguard: ignore[{','.join(sorted(rules))}] has no reason; "
                f"a free text reason after the bracket is required"
            )
            if strict:
                continue
        found[number] = InlineSuppression(
            line=number, rules=rules, reason=reason, raw=text.strip()
        )
    return found, problems


def inline_match(
    suppressions: dict[int, InlineSuppression],
    rule: str,
    lines: Iterable[int],
) -> InlineSuppression | None:
    """A suppression applies to its own line and the line immediately above."""
    for line in lines:
        for candidate in (line, line - 1):
            entry = suppressions.get(candidate)
            if entry is not None and entry.covers(rule):
                return entry
    return None


@dataclass
class ConfigSuppression:
    rule: str
    reason: str
    handler: str | None = None
    file: str | None = None

    def matches(self, *, rule: str, file: str, handler: str) -> bool:
        if not (self.rule == rule or self.rule == WILDCARD):
            return False
        if self.handler:
            # "shop/routes/admin.py::bulk_import" or "module:qualname"
            want_file, _, want_name = self.handler.partition("::")
            if want_name:
                if not file.replace("\\", "/").endswith(want_file.replace("\\", "/")):
                    return False
                return handler.rsplit(":", 1)[-1].split(".")[-1] == want_name
            return self.handler in handler
        if self.file:
            return file.replace("\\", "/").endswith(self.file.replace("\\", "/"))
        return True
