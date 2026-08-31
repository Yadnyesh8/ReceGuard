"""MCP adapter.

Version pinning is deliberate. The Python SDK's v2 line is the current stable
release and supports the 2026-07-28 specification along with every earlier
revision; in v2 the ``FastMCP`` class became ``MCPServer`` while the decorator
API is unchanged. Because ``pip install mcp`` now resolves to 2.x, a project
not ready to migrate must pin an upper bound such as ``mcp>=1.28,<2``. This
module works with either, resolved once at import.

Two things the 2026 revision changes that matter here:

* the protocol is stateless (no initialize handshake, no protocol level
  session), so a server can sit behind a plain round robin load balancer,
* MCP level logging is deprecated, with stderr for stdio transports and
  OpenTelemetry as the documented observability path. The run log is therefore
  NOT built on ``ctx.info``.

Structured tool output arrived in the 2025-06-18 revision and is generated
automatically from return types with a JSON object representation, so returning
a Pydantic model gives agents machine readable results with no extra work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from pydantic import BaseModel, Field

from .config import find_config, load_config
from .contracts import Confidence, Mode, Patch, ScanResult, Verdict
from .errors import RaceGuardError

# stderr, per the 2026 guidance. Never ctx.info.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("raceguard.mcp")

# `pip install mcp` resolves to 2.x, where FastMCP became MCPServer while the
# decorator API stayed the same. A project not ready to migrate pins
# `mcp>=1.28,<2`, so both spellings are supported and resolved once, here.
try:  # SDK v2
    from mcp.server import MCPServer as _Server

    SDK_MAJOR = 2
except ImportError:  # pragma: no cover - SDK v1 fallback
    from mcp.server.fastmcp import (
        FastMCP as _Server,  # type: ignore[assignment,no-redef,attr-defined]
    )

    SDK_MAJOR = 1

mcp = _Server("raceguard")


# --------------------------------------------------------------------------
# Section 12.3: the MCP surface has its own budget on top of the governor.
#
# An agent in a loop calls a tool far more often than a human runs a CLI, and
# `verify_handler` boots a database. Without these caps the first agent that
# decides to "check every handler" turns a laptop into a load generator.
# --------------------------------------------------------------------------

VERIFY_CALLS_PER_SESSION = int(os.environ.get("RACEGUARD_MCP_SESSION_CAP", "20"))
VERIFY_CALLS_PER_TARGET_PER_HOUR = int(os.environ.get("RACEGUARD_MCP_TARGET_CAP", "6"))
DEFAULT_MCP_TRIALS = 10
DEFAULT_MCP_MODE: Mode = "inprocess"


def project_root(path: str | Path) -> Path:
    """One stable anchor for every tool call in a session.

    Finding ids are sha1(file + qualname + pattern) with the file recorded
    relative to the scan root. Anchoring analyze_handler to one directory and
    suggest_fix to another therefore produces two different ids for the same
    defect, and the handoff between the tools silently fails. The anchor is the
    config directory, else the git root, else the path itself.
    """
    start = Path(path).resolve()
    if start.is_file():
        start = start.parent
    config = find_config(start)
    if config is not None:
        return config.parent
    for directory in [start, *start.parents]:
        if (directory / ".git").exists():
            return directory
    return start


class Budget:
    def __init__(self) -> None:
        self.session_calls = 0
        self.per_target: dict[str, deque[float]] = defaultdict(deque)

    def check(self, target_id: str) -> str | None:
        """Returns a structured refusal string, or None when the call may run."""
        if self.session_calls >= VERIFY_CALLS_PER_SESSION:
            return (
                f"verify_handler is capped at {VERIFY_CALLS_PER_SESSION} calls per "
                f"session and that cap is reached. Each call boots an ephemeral "
                f"PostgreSQL and fires concurrent requests. Use analyze_handler, "
                f"which is free, or run `raceguard verify` from a terminal."
            )
        now = time.monotonic()
        recent = self.per_target[target_id]
        while recent and now - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= VERIFY_CALLS_PER_TARGET_PER_HOUR:
            wait = int((3600 - (now - recent[0])) / 60)
            return (
                f"target {target_id!r} has been verified "
                f"{VERIFY_CALLS_PER_TARGET_PER_HOUR} times in the last hour, which "
                f"is the cap. Try again in about {wait} minutes, or change the code "
                f"first: re-verifying unchanged code produces the same answer."
            )
        return None

    def record(self, target_id: str) -> None:
        self.session_calls += 1
        self.per_target[target_id].append(time.monotonic())


budget = Budget()


# --------------------------------------------------------------------------
# Structured results
# --------------------------------------------------------------------------


class ToolError(BaseModel):
    """A refusal an agent can act on, rather than an exception it cannot."""

    error: str
    hint: str | None = None
    retryable: bool = False


class AnalyzeResult(BaseModel):
    findings: list[dict] = Field(default_factory=list)
    files_scanned: int = 0
    duration_s: float = 0.0
    summary: str = ""
    note: str = (
        "A finding is a static suspicion, not a result. Call verify_handler to "
        "find out whether it reproduces, and note that no finding does not mean "
        "the code is correct."
    )


class VerifyResult(BaseModel):
    verdict: dict | None = None
    summary: str = ""
    error: str | None = None
    note: str = (
        "One violating trial is a proof of the defect. Zero violating trials is "
        "evidence bounded by the trial count, seed, concurrency, mode and "
        "amplification setting; it is not a proof of correctness."
    )


# --------------------------------------------------------------------------
# Tools. The first line of each description states the cost, because that
# description is what the model reads when deciding whether to call it.
# --------------------------------------------------------------------------


@mcp.tool()
def analyze_handler(path: str) -> AnalyzeResult:
    """CHEAP (milliseconds, no Docker): static scan for read-modify-write windows.

    Safe to call on every file save. Reports where a handler reads shared state,
    decides on it, and writes it back with nothing preventing a concurrent
    request entering the gap. Findings are suspicions; use verify_handler to
    learn whether one actually reproduces.
    """
    from .scanner import ScanOptions
    from .scanner import scan as run_scan

    root = project_root(path)
    result: ScanResult = run_scan(
        [path],
        ScanOptions(
            min_confidence=Confidence.low,
            cache_dir=root / ".raceguard" / "cache",
            root=root,
        ),
    )
    counts: dict[str, int] = {}
    for finding in result.findings:
        counts[finding.confidence.value] = counts.get(finding.confidence.value, 0) + 1
    summary = (
        ", ".join(f"{count} {level}" for level, count in sorted(counts.items()))
        or "no read-modify-write windows matched"
    )
    return AnalyzeResult(
        findings=[f.model_dump(mode="json") for f in result.findings],
        files_scanned=result.files_scanned,
        duration_s=result.duration_s,
        summary=summary,
    )


@mcp.tool()
def verify_handler(
    target_id: str,
    trials: int = DEFAULT_MCP_TRIALS,
    config_path: str | None = None,
) -> VerifyResult:
    """EXPENSIVE (about 15 seconds): boots an ephemeral PostgreSQL and fires
    concurrent requests at one target.

    Call this only for a target already flagged by analyze_handler. It is capped
    per session and per target per hour; exceeding the cap returns a structured
    explanation rather than queueing. Defaults to inprocess mode with 10 trials
    so the editor loop stays responsive; inprocess cannot reproduce races that
    depend on more than one process, and the verdict says so.
    """
    from .harness import start_run, verify_target
    from .synth import derive_for_target

    refusal = budget.check(target_id)
    if refusal:
        return VerifyResult(error=refusal, summary="refused: budget")

    try:
        config = load_config(config_path)
    except RaceGuardError as exc:
        return VerifyResult(error=exc.message, summary="refused: configuration")

    config.defaults.mode = DEFAULT_MCP_MODE
    try:
        target = config.target(target_id)
    except RaceGuardError as exc:
        return VerifyResult(error=exc.render(), summary="refused: unknown target")

    budget.record(target_id)
    context = start_run(config)
    try:
        invariant = derive_for_target(
            config, target, admin_url=context.admin_url, template=config.database.template_name
        )
        verdict: Verdict = asyncio.run(
            verify_target(context, target, invariant, trials=trials, amplify=True)
        )
    except RaceGuardError as exc:
        return VerifyResult(error=exc.render(), summary="aborted")
    finally:
        context.cleanup()

    return VerifyResult(verdict=verdict.model_dump(mode="json"), summary=verdict.summary())


@mcp.tool()
def suggest_fix(finding_id: str, path: str = ".") -> dict:
    """CHEAP (milliseconds): returns an unapplied unified diff plus a rationale.

    Never writes to disk. The strategy is chosen by a decision table and the
    transformation is done by libcst, so the emitted code is mechanical and
    reviewable; only the rationale prose is written by a model. When a strategy
    needs a schema change the migration travels with the patch, and the code
    change alone is not a fix.
    """
    from .patch import propose
    from .scanner import ScanOptions
    from .scanner import scan as run_scan
    from .synth import from_config

    root = project_root(path)
    result = run_scan([str(root)], ScanOptions(min_confidence=Confidence.low, root=root))
    finding = next((f for f in result.findings if f.id == finding_id), None)
    if finding is None:
        return ToolError(
            error=f"no finding with id {finding_id!r} in {path}",
            hint="run analyze_handler first and use an id from its output",
        ).model_dump()

    invariant = None
    config_path = find_config(path)
    if config_path is not None:
        try:
            config = load_config(str(config_path))
            invariants = {t.invariant.table: from_config(t) for t in config.targets if t.invariant}
            invariant = next((invariants.get(t) for t in finding.tables if t in invariants), None)
        except RaceGuardError:
            invariant = None

    patch: Patch = propose(finding, invariant, root=root)
    return patch.model_dump(mode="json")


@mcp.tool()
def check_hygiene(path: str) -> dict:
    """CHEAP (milliseconds, no Docker): idempotency, rate limiting, and
    optimistic concurrency checks, one layer up from the SQL.

    Includes the check-then-act race inside an idempotency implementation
    itself: looking the key up, doing the work, then storing the key means two
    concurrent replays both miss the lookup and both proceed.
    """
    from .hygiene import check

    findings = check([path], root=project_root(path))
    return {
        "findings": [f.model_dump(mode="json") for f in findings],
        "count": len(findings),
    }


def main() -> None:
    log.info("raceguard MCP server starting (SDK v%s)", SDK_MAJOR)
    mcp.run()


if __name__ == "__main__":
    main()
