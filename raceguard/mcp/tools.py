"""The tools. The first line of every description states the cost, because
that description is what a model reads when deciding whether to call it.

Every tool returns a Pydantic model or a ``ToolError``; nothing raises across
the protocol boundary, so a host never sees a stack trace and an agent always
gets a hint it can act on. Nothing here writes to the project tree.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..config import find_config, load_config
from ..contracts import Confidence, Patch, ScanResult, Verdict
from ..errors import RaceGuardError
from .budget import current_client
from .runtime import Runtime
from .sandbox import SandboxViolation

log = logging.getLogger("raceguard.mcp.tools")

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
VERIFY = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


# --------------------------------------------------------------------------
# Structured results
# --------------------------------------------------------------------------


class ToolError(BaseModel):
    """A refusal an agent can act on, rather than an exception it cannot."""

    error: str
    hint: str | None = None
    retryable: bool = False
    retry_after_s: int | None = None


class AnalyzeResult(BaseModel):
    findings: list[dict] = Field(default_factory=list)
    files_scanned: int = 0
    duration_s: float = 0.0
    summary: str = ""
    root: str = ""
    note: str = (
        "A finding is a static suspicion, not a result. Call verify_handler to "
        "find out whether it reproduces, and note that no finding does not mean "
        "the code is correct."
    )


class VerifyResult(BaseModel):
    verdict: dict | None = None
    summary: str = ""
    # Promoted out of verdict.notes: a warning here means "not reproduced" is
    # probably a false negative rather than good news. Read it before acting.
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    retry_after_s: int | None = None
    note: str = (
        "One violating trial is a proof of the defect. Zero violating trials is "
        "evidence bounded by the trial count, seed, concurrency, mode and "
        "amplification setting; it is not a proof of correctness."
    )


class HygieneResult(BaseModel):
    findings: list[dict] = Field(default_factory=list)
    count: int = 0


class TargetsResult(BaseModel):
    config_path: str | None = None
    targets: list[dict] = Field(default_factory=list)
    note: str = "Target ids are what verify_handler takes."


class RunResult(BaseModel):
    run_id: str
    files: dict[str, object] = Field(default_factory=dict)


class DoctorResult(BaseModel):
    checks: list[dict] = Field(default_factory=list)
    ok: bool = True
    project_root: str = ""
    verify_enabled: bool = True


# --------------------------------------------------------------------------


def register(mcp, runtime: Runtime) -> None:
    settings = runtime.settings
    sandbox = runtime.sandbox
    budget = runtime.budget
    metrics = runtime.metrics

    def _sandboxed(path: str | None) -> Path | ToolError:
        try:
            return sandbox.resolve(path)
        except SandboxViolation as exc:
            return ToolError(
                error=str(exc),
                hint=(
                    f"paths must stay inside {sandbox.root}; start the server with "
                    f"--project-root to change it"
                ),
            )
        except OSError as exc:
            return ToolError(error=f"cannot resolve {path!r}: {exc}")

    def _scan(path: Path) -> ScanResult:
        from ..scanner import ScanOptions
        from ..scanner import scan as run_scan

        root = sandbox.anchor(path)
        cache = root / ".raceguard" / "cache"
        return run_scan(
            [str(path)],
            ScanOptions(
                min_confidence=Confidence.low,
                cache_dir=cache if _writable(cache) else None,
                root=root,
            ),
        )

    # -- cheap ---------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    def analyze_handler(path: str = ".") -> AnalyzeResult | ToolError:
        """CHEAP (milliseconds, no Docker): static scan for read-modify-write windows.

        Safe to call on every file save. Reports where a handler reads shared
        state, decides on it, and writes it back with nothing preventing a
        concurrent request entering the gap. Findings are suspicions; use
        verify_handler to learn whether one actually reproduces.
        """
        resolved = _sandboxed(path)
        if isinstance(resolved, ToolError):
            metrics.observe_call("analyze_handler", "refused")
            return resolved
        try:
            result = _scan(resolved)
        except RaceGuardError as exc:
            metrics.observe_call("analyze_handler", "error")
            return ToolError(error=exc.message, hint=exc.hint)
        metrics.observe_call("analyze_handler", "ok")
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
            root=str(sandbox.anchor(resolved)),
        )

    @mcp.tool(annotations=READ_ONLY)
    def check_hygiene(path: str = ".") -> HygieneResult | ToolError:
        """CHEAP (milliseconds, no Docker): idempotency, rate limiting, and
        optimistic concurrency checks, one layer up from the SQL.

        Includes the check-then-act race inside an idempotency implementation
        itself: looking the key up, doing the work, then storing the key means
        two concurrent replays both miss the lookup and both proceed.
        """
        from ..hygiene import check

        resolved = _sandboxed(path)
        if isinstance(resolved, ToolError):
            metrics.observe_call("check_hygiene", "refused")
            return resolved
        findings = check([str(resolved)], root=sandbox.anchor(resolved))
        metrics.observe_call("check_hygiene", "ok")
        return HygieneResult(
            findings=[f.model_dump(mode="json") for f in findings], count=len(findings)
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_finding(finding_id: str, path: str = ".") -> dict | ToolError:
        """CHEAP (milliseconds): one finding by id from a fresh scan of the project.

        Ids are stable across runs for unchanged code, so an id from an earlier
        analyze_handler call can be looked up here.
        """
        resolved = _sandboxed(path)
        if isinstance(resolved, ToolError):
            return resolved
        result = _scan(sandbox.anchor(resolved))
        finding = next((f for f in result.findings if f.id == finding_id), None)
        metrics.observe_call("get_finding", "ok" if finding else "missing")
        if finding is None:
            return ToolError(
                error=f"no finding with id {finding_id!r} under {sandbox.anchor(resolved)}",
                hint="run analyze_handler and use an id from its output",
            )
        return finding.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    def list_targets(config_path: str | None = None) -> TargetsResult | ToolError:
        """CHEAP (milliseconds): the targets in raceguard.yaml, with their routes
        and invariants, redacted. These ids are what verify_handler takes."""
        resolved = _sandboxed(config_path) if config_path else None
        if isinstance(resolved, ToolError):
            return resolved
        found = resolved if resolved else find_config(sandbox.root)
        if found is None or not Path(found).is_file():
            metrics.observe_call("list_targets", "missing")
            return ToolError(
                error=f"no raceguard.yaml found under {sandbox.root}",
                hint="run `raceguard init` to generate one from a scan",
            )
        try:
            config = load_config(str(found))
        except RaceGuardError as exc:
            metrics.observe_call("list_targets", "error")
            return ToolError(error=exc.message, hint=exc.hint)
        metrics.observe_call("list_targets", "ok")
        redacted = config.redacted()
        return TargetsResult(config_path=str(found), targets=list(redacted.get("targets", [])))

    @mcp.tool(annotations=READ_ONLY)
    def suggest_fix(finding_id: str, path: str = ".") -> dict | ToolError:
        """CHEAP (milliseconds): returns an unapplied unified diff plus a rationale.

        Never writes to disk. The strategy is chosen by a decision table and the
        transformation is done by libcst, so the emitted code is mechanical and
        reviewable. When a strategy needs a schema change the migration travels
        with the patch, and the code change alone is not a fix. Apply it with
        `raceguard fix --apply` from a terminal, which reverifies and runs your
        tests before keeping anything.
        """
        from ..patch import propose
        from ..synth import from_config

        resolved = _sandboxed(path)
        if isinstance(resolved, ToolError):
            metrics.observe_call("suggest_fix", "refused")
            return resolved
        root = sandbox.anchor(resolved)
        result = _scan(root)
        finding = next((f for f in result.findings if f.id == finding_id), None)
        if finding is None:
            metrics.observe_call("suggest_fix", "missing")
            return ToolError(
                error=f"no finding with id {finding_id!r} under {root}",
                hint="run analyze_handler first and use an id from its output",
            )
        invariant = None
        config_path = find_config(root)
        if config_path is not None:
            try:
                config = load_config(str(config_path))
                by_table = {
                    t.invariant.table: from_config(t) for t in config.targets if t.invariant
                }
                invariant = next((by_table.get(t) for t in finding.tables if t in by_table), None)
            except RaceGuardError:
                invariant = None
        try:
            patch: Patch = propose(finding, invariant, root=root)
        except RaceGuardError as exc:
            metrics.observe_call("suggest_fix", "error")
            return ToolError(error=exc.message, hint=exc.hint)
        metrics.observe_call("suggest_fix", "ok")
        return patch.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    def get_run(run_id: str, path: str = ".") -> RunResult | ToolError:
        """CHEAP (milliseconds): the artifacts a previous CLI run recorded under
        .raceguard/runs/, by run id or its full folder name."""
        import json

        resolved = _sandboxed(path)
        if isinstance(resolved, ToolError):
            return resolved
        runs = sandbox.anchor(resolved) / ".raceguard" / "runs"
        match = next(
            (
                d
                for d in sorted(runs.glob("*"))
                if d.is_dir() and (d.name == run_id or d.name.endswith(run_id))
            ),
            None,
        )
        if match is None:
            metrics.observe_call("get_run", "missing")
            return ToolError(error=f"no run {run_id!r} under {runs}")
        files: dict[str, object] = {}
        for file in sorted(match.iterdir()):
            if file.suffix == ".json":
                try:
                    files[file.name] = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    files[file.name] = "<unreadable>"
            elif file.is_file() and file.stat().st_size < 64_000:
                files[file.name] = file.read_text(encoding="utf-8", errors="replace")
        metrics.observe_call("get_run", "ok")
        return RunResult(run_id=match.name, files=files)

    @mcp.tool(annotations=READ_ONLY)
    def doctor() -> DoctorResult:
        """CHEAP (seconds): can this server scan, and can it verify? Checks the
        interpreter, the scanner imports, Docker, and the project's config."""
        checks: list[dict] = []
        checks.append(
            {"check": "python", "ok": sys.version_info >= (3, 11), "detail": sys.version.split()[0]}
        )
        try:
            import libcst  # noqa: F401

            checks.append({"check": "scanner", "ok": True, "detail": "libcst importable"})
        except ImportError as exc:
            checks.append({"check": "scanner", "ok": False, "detail": str(exc)})
        if settings.verify_enabled:
            checks.append(
                {"check": "docker", "ok": docker_reachable(), "detail": "needed by verify_handler"}
            )
        checks.append(
            {"check": "project root", "ok": sandbox.root.is_dir(), "detail": str(sandbox.root)}
        )
        config = find_config(sandbox.root)
        checks.append(
            {
                "check": "raceguard.yaml",
                "ok": config is not None,
                "detail": str(config) if config else "not found",
            }
        )
        metrics.observe_call("doctor", "ok")
        return DoctorResult(
            checks=checks,
            ok=all(c["ok"] for c in checks),
            project_root=str(sandbox.root),
            verify_enabled=settings.verify_enabled,
        )

    # -- expensive -----------------------------------------------------

    @mcp.tool(annotations=VERIFY)
    def verify_handler(
        target_id: str,
        trials: int = settings.default_trials,
        config_path: str | None = None,
    ) -> VerifyResult:
        """EXPENSIVE (about 15 seconds, needs Docker): boots an ephemeral
        PostgreSQL and fires concurrent requests at one target.

        Call this only for a target already flagged by analyze_handler. It is
        capped per client session and per target per hour, and only a bounded
        number run at once server-wide; exceeding either returns a structured
        explanation with a retry hint rather than queueing. Defaults to
        inprocess mode so the loop stays responsive; inprocess cannot reproduce
        races that need more than one process, and the verdict says so.
        """
        if not settings.verify_enabled:
            metrics.observe_call("verify_handler", "disabled")
            return VerifyResult(
                error="verify_handler is disabled on this server", summary="refused: disabled"
            )

        client = current_client.get()
        refusal = budget.check(client, target_id)
        if refusal:
            metrics.observe_call("verify_handler", "refused")
            return VerifyResult(
                error=refusal.reason, summary="refused: budget", retry_after_s=refusal.retry_after_s
            )

        resolved = _sandboxed(config_path) if config_path else None
        if isinstance(resolved, ToolError):
            metrics.observe_call("verify_handler", "refused")
            return VerifyResult(error=resolved.error, summary="refused: sandbox")
        try:
            config = load_config(str(resolved) if resolved else None, start=sandbox.root)
        except RaceGuardError as exc:
            metrics.observe_call("verify_handler", "error")
            return VerifyResult(error=exc.render(), summary="refused: configuration")
        try:
            target = config.target(target_id)
        except RaceGuardError as exc:
            metrics.observe_call("verify_handler", "error")
            return VerifyResult(error=exc.render(), summary="refused: unknown target")

        if not budget.acquire():
            metrics.observe_call("verify_handler", "busy")
            return VerifyResult(
                error=(
                    f"{budget.in_flight} verification(s) already running and the server "
                    f"allows {settings.max_concurrent_verifies} at once; try again shortly"
                ),
                summary="refused: busy",
                retry_after_s=20,
            )

        from ..harness import start_run, verify_target
        from ..synth import derive_for_target

        config.defaults.mode = "inprocess"
        trials = max(1, min(trials, 50))
        budget.record(client, target_id)
        started = time.monotonic()
        context = None
        try:
            context = start_run(config)
            invariant = derive_for_target(
                config, target, admin_url=context.admin_url, template=config.database.template_name
            )
            verdict: Verdict = asyncio.run(
                verify_target(context, target, invariant, trials=trials, amplify=True)
            )
        except RaceGuardError as exc:
            metrics.observe_call("verify_handler", "aborted")
            return VerifyResult(error=exc.render(), summary="aborted")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback over the wire
            log.exception("verify_handler failed for %s", target_id)
            metrics.observe_call("verify_handler", "error")
            return VerifyResult(error=f"verification failed: {exc!r}", summary="aborted")
        finally:
            if context is not None:
                context.cleanup()
            budget.release()
            metrics.observe_verify(time.monotonic() - started)

        metrics.observe_call("verify_handler", "violated" if verdict.violated else "clean")
        # A warning note means the run probably measured the wrong thing (an
        # ORM attribute decrement makes both requests write the same value, so
        # a non_negative check never trips while the resource is oversold).
        # Buried in verdict.notes it is invisible: an agent reads `summary`,
        # sees a clean "not reproduced", and concludes there is no race. That
        # is the exact false negative this tool exists to prevent, so the
        # warning is promoted to the summary and to its own field.
        warnings = [n for n in verdict.notes if n.startswith("WARNING")]
        summary = verdict.summary()
        if warnings:
            summary = (
                f"{summary} -- {len(warnings)} WARNING(S): read `warnings` before trusting this"
            )
        return VerifyResult(
            verdict=verdict.model_dump(mode="json"),
            summary=summary,
            warnings=warnings,
        )


def docker_reachable(timeout_s: float = 5.0) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=timeout_s).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
