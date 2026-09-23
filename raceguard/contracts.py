"""Every object that crosses a module boundary.

Two invariants of this file, per spec section 5:

1. ``Invariant.check_sql`` returns rows ONLY on violation. One convention,
   enforced in one place (``synth.validate``).
2. ``Verdict.violated is False`` means "not reproduced under these conditions".
   It never means "safe". Every renderer must phrase it that way; there is a
   test asserting the word "safe" never appears in report output.

This module imports nothing from the rest of the project. That is enforced by
``tests/test_import_discipline.py``.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

Pattern = Literal[
    "lost_update",
    "oversell",
    "single_use",
    "duplicate_insert",
    "aggregate_limit",
]

InvariantKind = Literal[
    "non_negative",
    "conservation",
    "single_use",
    "duplicate_insert",
    "aggregate_limit",
]

HygieneRule = Literal[
    "missing_idempotency_key",
    "unsafe_idempotency_check",
    "missing_rate_limit",
    "missing_optimistic_concurrency",
    "unbounded_retry",
    "swallowed_serialization_failure",
]

Strategy = Literal[
    "atomic_update",
    "select_for_update",
    "insert_on_conflict",
    "advisory_lock",
    "optimistic_version",
    "serializable_retry",
]

SyncStrategy = Literal["barrier", "last_byte", "single_packet"]
Mode = Literal["container", "inprocess"]


class Confidence(StrEnum):
    """How much of the read-decide-write window the scanner could actually resolve."""

    high = "high"  # read, decide, write all linked in one scope, no guard
    medium = "medium"  # write or guard crosses a call boundary
    low = "low"  # something unresolvable is in the path

    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


# --------------------------------------------------------------------------
# Static analysis
# --------------------------------------------------------------------------


class CodeSpan(BaseModel):
    file: str
    start_line: int
    end_line: int
    snippet: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.file}:{self.start_line}"


class Finding(BaseModel):
    """A static suspicion. Never a result. See ``Verdict`` for results."""

    id: str  # sha1(file + qualname + pattern)[:12], stable across runs
    handler: str  # "shop.routes.orders:create_order"
    route: str | None = None  # "POST /orders"
    pattern: Pattern
    read_site: CodeSpan
    decision_site: CodeSpan | None = None
    write_site: CodeSpan
    guard_found: str | None = None  # name of the guard detected, if any
    guard_partial: str | None = None  # "serializable isolation, no retry wrapper"
    confidence: Confidence
    explanation: str  # plain sentence; names what could not be resolved
    tables: list[str] = Field(default_factory=list)  # seeds the synthesizer
    columns: list[str] = Field(default_factory=list)
    is_async: bool = True
    suppressed: bool = False
    suppression_reason: str | None = None

    @property
    def fingerprint(self) -> str:
        """The baseline key: stable across line shifts, distinct across handlers.

        ``id`` includes the read line so two same-named handlers never collide,
        but that makes it wrong for a baseline: editing a line above the handler
        changes the id and resurrects a finding somebody already accepted. The
        fingerprint drops the line and adds the route instead, which is what
        tells two same-named handlers apart without moving when the file does.
        """
        key = f"{self.read_site.file}|{self.handler}|{self.pattern}|{self.route or ''}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @staticmethod
    def make_id(file: str, qualname: str, pattern: str, read_line: int = 0) -> str:
        # read_line disambiguates two handlers that share a qualname (a name
        # reused in one module or across router files). Without it their ids
        # collide, and a colliding id means suppressing one suppresses both and
        # GitHub code scanning dedupes two real findings into one.
        return hashlib.sha1(f"{file}|{qualname}|{pattern}|{read_line}".encode()).hexdigest()[:12]


class ScanResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    hygiene: list[HygieneFinding] = Field(default_factory=list)
    files_scanned: int = 0
    duration_s: float = 0.0
    suppressed: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Invariant synthesis
# --------------------------------------------------------------------------


class Invariant(BaseModel):
    kind: InvariantKind
    table: str
    column: str | None = None
    predicate: str  # "items.stock >= 0", for humans
    check_sql: str  # MUST return zero rows when the invariant holds
    params: dict = Field(default_factory=dict)
    source: Literal["config", "spec", "schema"]
    derivation: str  # the sentence, constraint, or config line it came from
    critic_note: str | None = None


# --------------------------------------------------------------------------
# Dynamic verification
# --------------------------------------------------------------------------


class TrialRecord(BaseModel):
    index: int
    concurrency: int
    status_codes: list[int] = Field(default_factory=list)
    arrival_spread_ms: float = 0.0  # max minus min server receive time, when measurable
    final_state: dict = Field(default_factory=dict)
    violated: bool = False
    error: str | None = None
    duration_s: float = 0.0

    @property
    def successes(self) -> int:
        return sum(1 for code in self.status_codes if 200 <= code < 300)

    @property
    def usable(self) -> bool:
        """A trial where every request 5xx'd proves nothing and is excluded."""
        if self.error is not None:
            return False
        if not self.status_codes:
            return False
        return any(code < 500 for code in self.status_codes)


class Verdict(BaseModel):
    finding_id: str
    target_id: str
    invariant: Invariant
    violated: bool
    violating_trials: int
    total_trials: int
    usable_trials: int = 0
    reproduction_rate: float = 0.0
    ci95: tuple[float, float] = (0.0, 0.0)  # Wilson interval, reported not asserted
    amplified: bool = False
    amplify_ms: int = 0
    sync: SyncStrategy = "barrier"
    concurrency: int = 0
    mode: Mode = "inprocess"
    duration_s: float = 0.0
    trials: list[TrialRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        """The only sentence allowed to describe a verdict. Never says 'safe'."""
        if self.violated:
            return (
                f"invariant violated in {self.violating_trials} of {self.usable_trials} "
                f"usable trials ({self.reproduction_rate:.0%})"
            )
        return (
            f"not reproduced in {self.usable_trials} trials at concurrency "
            f"{self.concurrency} ({self.mode} mode"
            f"{', amplified' if self.amplified else ''})"
        )


# --------------------------------------------------------------------------
# Remediation
# --------------------------------------------------------------------------


class Migration(BaseModel):
    kind: Literal["unique_index", "check_constraint", "version_column"]
    statement: str  # raw SQL
    alembic: str  # equivalent op.* call


class Patch(BaseModel):
    finding_id: str
    strategy: Strategy
    diff: str  # unified diff; never written without --apply
    rationale: str
    migration: Migration | None = None
    reverified: Verdict | None = None
    test_suite_passed: bool | None = None
    applied: bool = False
    notes: list[str] = Field(default_factory=list)


class HygieneFinding(BaseModel):
    id: str
    route: str
    rule: HygieneRule
    severity: Literal["high", "medium", "low"]
    explanation: str
    remediation_available: bool = False
    span: CodeSpan | None = None

    @staticmethod
    def make_id(file: str, route: str, rule: str) -> str:
        return hashlib.sha1(f"{file}|{route}|{rule}".encode()).hexdigest()[:12]


class RunArtifact(BaseModel):
    """What ``.raceguard/runs/<id>/` records. Bug reports become 'attach this'."""

    run_id: str
    started_at: str
    tool_version: str
    command: str
    scan: ScanResult | None = None
    invariants: dict[str, Invariant] = Field(default_factory=dict)
    verdicts: list[Verdict] = Field(default_factory=list)
    patches: list[Patch] = Field(default_factory=list)
    governor: dict = Field(default_factory=dict)
    exit_code: int = 0


ScanResult.model_rebuild()
