"""The CI engine: one run, every provider.

``run(options)`` scans, optionally verifies what changed, decides what gates
the build, and writes every output the host can consume. The GitHub Action,
the GitLab template and a bare ``raceguard ci`` in Jenkins all call this and
get the same answer. Provider differences are confined to ``providers``.

The order of operations is deliberate: every report is written BEFORE the
exit code is decided, so a gated build still ships its SARIF and summary.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, find_config, load_config
from ..contracts import Confidence, Finding, ScanResult, Verdict
from ..errors import EXIT_CONFIG, EXIT_FINDINGS, EXIT_INFRA, EXIT_OK, RaceGuardError
from ..report import codequality as codequality_report
from ..report import json as json_report
from ..report import sarif as sarif_report
from ..report import text as text_report
from . import providers
from .baseline import Baseline
from .comment import CommentResult, post_sticky
from .providers import ProviderContext

log = logging.getLogger("raceguard.ci")

TOOL_VERSION = "0.1.0"
ANNOTATION_CAP = 50


@dataclass
class CIOptions:
    path: str = "."
    provider: str = "auto"
    base: str | None = None
    changed_only: bool | None = None  # None: provider default
    fail_on: str = "high"
    baseline: str | None = None
    verify: bool = False
    verify_required: bool = False
    budget_seconds: int | None = None
    hygiene: bool = True
    sarif: str | None = "raceguard.sarif"
    json_path: str | None = None
    codequality: str | None = None
    summary_path: str | None = None
    annotations: bool = True
    comment: bool = False
    category: str = "raceguard"
    env: Mapping[str, str] | None = None


@dataclass
class SkippedTarget:
    target_id: str
    reason: str


@dataclass
class CIResult:
    provider: ProviderContext
    scan: ScanResult
    verdicts: list[Verdict] = field(default_factory=list)
    gated: list[Finding] = field(default_factory=list)
    baselined: list[Finding] = field(default_factory=list)
    out_of_diff: list[Finding] = field(default_factory=list)
    skipped: list[SkippedTarget] = field(default_factory=list)
    stale_baseline: set[str] = field(default_factory=set)
    changed_files: set[str] = field(default_factory=set)
    changed_only: bool = False
    notices: list[str] = field(default_factory=list)
    comment: CommentResult | None = None
    exit_code: int = EXIT_OK
    summary_markdown: str = ""
    written: dict[str, str] = field(default_factory=dict)

    @property
    def violations(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.violated]


# --------------------------------------------------------------------------


def run(options: CIOptions) -> CIResult:
    context = providers.detect(options.env, forced=options.provider)
    root = Path(options.path).resolve()

    base = options.base or context.base_ref
    changed, notice = providers.changed_files(base, cwd=root)
    changed_only = (
        options.changed_only if options.changed_only is not None else context.changed_only_default
    )
    notices: list[str] = []
    if notice:
        notices.append(notice)
    if changed_only and not changed:
        if base:
            notices.append("changed-only requested but the diff is empty; gating on every file")
        else:
            notices.append("changed-only requested with no diff base; gating on every file")
        changed_only = False

    scan = _scan(options, root)
    result = CIResult(
        provider=context,
        scan=scan,
        changed_files=changed,
        changed_only=changed_only,
        notices=notices,
    )

    baseline = None
    if options.baseline:
        baseline = Baseline.load(root / options.baseline)
        result.stale_baseline = baseline.stale(scan.findings)

    _gate(result, options, baseline)

    if options.verify:
        _verify(result, options, root, base)

    result.summary_markdown = summary(result)
    _write_outputs(result, options, root)
    _speak_to_provider(result, options)

    if options.comment:
        result.comment = post_sticky(context, result.summary_markdown)
        if not result.comment.posted:
            result.notices.append(
                f"sticky comment {result.comment.action}: {result.comment.reason}"
            )

    if result.exit_code == EXIT_OK and (result.gated or result.violations):
        result.exit_code = EXIT_FINDINGS
    return result


# --------------------------------------------------------------------------


def _scan(options: CIOptions, root: Path) -> ScanResult:
    from ..scanner import ScanOptions
    from ..scanner import scan as run_scan

    result = run_scan([str(root)], ScanOptions(min_confidence=Confidence.low, root=root))
    if options.hygiene:
        from ..hygiene import check

        result.hygiene = check([str(root)], root=root)
    return result


def _gate(result: CIResult, options: CIOptions, baseline: Baseline | None) -> None:
    if options.fail_on == "never":
        return
    threshold = Confidence(options.fail_on).rank()
    for finding in result.scan.findings:
        if finding.suppressed or finding.confidence.rank() < threshold:
            continue
        if baseline is not None and baseline.contains(finding):
            result.baselined.append(finding)
            continue
        if result.changed_only and finding.read_site.file not in result.changed_files:
            result.out_of_diff.append(finding)
            continue
        result.gated.append(finding)


def _verify(result: CIResult, options: CIOptions, root: Path, base: str | None) -> None:
    config_path = find_config(root)
    if config_path is None:
        result.notices.append("verify requested but no raceguard.yaml was found; skipped")
        if options.verify_required:
            result.exit_code = EXIT_CONFIG
        return
    if not _docker_reachable():
        result.notices.append(
            "verify requested but Docker is not reachable on this runner; skipped"
        )
        if options.verify_required:
            result.exit_code = EXIT_INFRA
        return
    try:
        config = load_config(str(config_path))
    except RaceGuardError as exc:
        result.notices.append(f"verify skipped: {exc.message}")
        result.exit_code = EXIT_CONFIG
        return

    if result.changed_files:
        ids = targets_in_diff(config, result.scan.findings, result.changed_files)
        if not ids:
            result.notices.append(
                "no changed handler maps to a configured target; nothing to verify"
            )
            return
    elif base is None and options.changed_only is False:
        ids = [t.id for t in config.targets]
    else:
        result.notices.append(
            "verification needs a diff base (or --all-files with --verify) so it "
            "does not run every target on every push; skipped"
        )
        return

    try:
        _run_targets(
            result,
            config,
            ids,
            options.budget_seconds,
            findings_by_target(config, result.scan.findings),
        )
    except RaceGuardError as exc:
        result.notices.append(f"verification aborted: {exc.message}")
        result.exit_code = EXIT_INFRA


def _run_targets(
    result: CIResult,
    config: Config,
    ids: list[str],
    budget_s: int | None,
    finding_of: dict[str, str] | None = None,
) -> None:
    import asyncio

    from ..harness import start_run, verify_target
    from ..synth import derive_for_target

    finding_of = finding_of or {}
    started = time.monotonic()
    context = start_run(config)
    try:
        for index, target_id in enumerate(ids):
            elapsed = time.monotonic() - started
            if budget_s is not None and elapsed > budget_s:
                for remaining in ids[index:]:
                    result.skipped.append(
                        SkippedTarget(
                            remaining, f"CI budget of {budget_s}s exhausted after {elapsed:.0f}s"
                        )
                    )
                break
            target = config.target(target_id)
            invariant = derive_for_target(
                config, target, admin_url=context.admin_url, template=config.database.template_name
            )
            result.verdicts.append(
                asyncio.run(
                    verify_target(
                        context,
                        target,
                        invariant,
                        # Without this the verdict is orphaned: every report
                        # keys its "VERIFIED" escalation on finding_id, so a
                        # reproduced violation would render as an ordinary
                        # static suspicion.
                        finding_id=finding_of.get(target_id, ""),
                        amplify=True,
                    )
                )
            )
    finally:
        context.cleanup()


def findings_by_target(config: Config, findings) -> dict[str, str]:
    """target id -> the id of the finding on that route, highest confidence first."""
    best: dict[str, tuple[int, str]] = {}
    by_route: dict[str, list] = {}
    for finding in findings:
        if finding.route:
            by_route.setdefault(finding.route, []).append(finding)
    for target in config.targets:
        for finding in by_route.get(target.route or "", []):
            rank = finding.confidence.rank()
            if target.id not in best or rank > best[target.id][0]:
                best[target.id] = (rank, finding.id)
    return {target_id: finding_id for target_id, (_, finding_id) in best.items()}


def targets_in_diff(config: Config, findings, touched: set[str]) -> list[str]:
    """Targets worth verifying for this diff.

    A target qualifies two ways. Either a FINDING sits on its route in a
    changed file, or the target's own configured handler file changed. The
    second clause is what keeps a "fix" honest: edit the handler so the
    scanner no longer recognises the pattern and the first clause goes quiet,
    which would let a still-racy handler through on a green static scan. The
    target is configured precisely because somebody decided this route needs
    proving, so a change to it gets proven.
    """
    if not touched:
        return []
    changed_routes = {f.route for f in findings if f.read_site.file in touched and f.route}
    selected: list[str] = []
    for target in config.targets:
        if target.route and target.route in changed_routes:
            selected.append(target.id)
        elif target.handler and _handler_file(target.handler) in touched:
            selected.append(target.id)
    return selected


def _handler_file(handler: str) -> str:
    """``shop/routes/orders.py::create_order`` -> ``shop/routes/orders.py``."""
    return handler.split("::", 1)[0]


def _docker_reachable(timeout_s: float = 10.0) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=timeout_s).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


def _write_outputs(result: CIResult, options: CIOptions, root: Path) -> None:
    if options.sarif:
        document = sarif_report.build(
            result.scan, verdicts=result.verdicts, category=options.category
        )
        path = _write(root, options.sarif, json.dumps(document, indent=2))
        result.written["sarif"] = str(path)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 10:
            result.notices.append(f"{path} is {size_mb:.1f} MB; GitHub rejects SARIF over 10 MB")
    if options.json_path:
        document = json_report.build(
            scan=result.scan, verdicts=result.verdicts, tool_version=TOOL_VERSION
        )
        document["ci"] = {
            "provider": result.provider.name,
            "changed_only": result.changed_only,
            "changed_files": sorted(result.changed_files),
            "gated": [f.id for f in result.gated],
            "baselined": [f.id for f in result.baselined],
            "out_of_diff": [f.id for f in result.out_of_diff],
            "skipped": [{"target": s.target_id, "reason": s.reason} for s in result.skipped],
            "stale_baseline": sorted(result.stale_baseline),
            "notices": result.notices,
        }
        result.written["json"] = str(
            _write(root, options.json_path, json.dumps(document, indent=2, default=str))
        )
    if options.codequality:
        result.written["codequality"] = str(
            _write(
                root,
                options.codequality,
                codequality_report.render(result.scan, verdicts=result.verdicts),
            )
        )
    if options.summary_path:
        result.written["summary"] = str(_write(root, options.summary_path, result.summary_markdown))


def _write(root: Path, target: str, content: str) -> Path:
    path = Path(target)
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _speak_to_provider(result: CIResult, options: CIOptions) -> None:
    env = result.provider.env
    if result.provider.name == "github":
        if options.annotations:
            for line in github_annotations(result):
                print(line)
        providers.write_github_summary(env, result.summary_markdown)
        providers.write_github_outputs(
            env,
            {
                "findings": len(result.scan.findings),
                "gated": len(result.gated),
                "violations": len(result.violations),
                "sarif": result.written.get("sarif", ""),
                "exit-code": _final_exit(result),
            },
        )
    for notice in result.notices:
        if result.provider.name == "github":
            print(f"::notice::{notice}")
        else:
            print(f"notice: {notice}", file=sys.stderr)


def _final_exit(result: CIResult) -> int:
    if result.exit_code != EXIT_OK:
        return result.exit_code
    return EXIT_FINDINGS if (result.gated or result.violations) else EXIT_OK


def github_annotations(result: CIResult) -> list[str]:
    """Inline annotations: gated findings as errors, the rest as notices.

    Capped, because GitHub stops rendering after fifty and a wall of notices
    hides the ones that matter.
    """
    lines: list[str] = []
    violated = {v.finding_id for v in result.violations}
    for finding in result.gated:
        level = "error"
        title = f"raceguard: {finding.pattern}"
        if finding.id in violated:
            title += " (verified)"
        lines.append(
            providers.github_annotation(
                level,
                finding.read_site.file,
                finding.read_site.start_line,
                title,
                finding.explanation,
            )
        )
    for finding in result.out_of_diff + result.baselined:
        lines.append(
            providers.github_annotation(
                "notice",
                finding.read_site.file,
                finding.read_site.start_line,
                f"raceguard: {finding.pattern} ({finding.confidence.value}, not gated)",
                finding.explanation,
            )
        )
    return lines[:ANNOTATION_CAP]


def summary(result: CIResult) -> str:
    """Markdown for the step summary, the sticky comment, and stdout."""
    scan = result.scan
    lines = ["## RaceGuard", ""]
    counts: dict[str, int] = {}
    for finding in scan.findings:
        counts[finding.confidence.value] = counts.get(finding.confidence.value, 0) + 1
    if counts:
        lines.append(
            "Findings: "
            + ", ".join(
                f"**{v}** {k}"
                for k, v in sorted(counts.items(), key=lambda kv: -Confidence(kv[0]).rank())
            )
            + f" in {scan.files_scanned} files."
        )
    else:
        lines.append(
            f"No read-modify-write windows matched the recognised patterns in "
            f"{scan.files_scanned} files. That is not the same as the code being free of races."
        )
    if result.changed_only:
        lines.append(f"Gating on the {len(result.changed_files)} changed file(s) only.")
    if result.gated:
        lines.append(f"**{len(result.gated)} finding(s) gate this build.**")
    lines.append("")

    if scan.findings:
        lines += ["| status | confidence | pattern | route | location |", "|---|---|---|---|---|"]
        gated_ids = {f.id for f in result.gated}
        baselined_ids = {f.id for f in result.baselined}
        for finding in scan.findings[:25]:
            if finding.id in gated_ids:
                status = "gated"
            elif finding.id in baselined_ids:
                status = "baseline"
            elif finding.suppressed:
                status = "suppressed"
            elif result.changed_only:
                status = "unchanged"
            else:
                status = "below gate"
            lines.append(
                f"| {status} | {finding.confidence.value} | `{finding.pattern}` | "
                f"`{finding.route or '?'}` | "
                f"`{finding.read_site.file}:{finding.read_site.start_line}` |"
            )
        if len(scan.findings) > 25:
            lines.append(f"| ... | | | | {len(scan.findings) - 25} more in the SARIF |")
        lines.append("")

    if scan.hygiene:
        lines.append(f"Hygiene: {len(scan.hygiene)} finding(s) (not gated).")
        lines.append("")

    if result.verdicts or result.skipped:
        lines += [
            "### Verification",
            "",
            "| target | result | rate | conditions |",
            "|---|---|---|---|",
        ]
        for verdict in result.verdicts:
            state = "**violated**" if verdict.violated else "not reproduced"
            lines.append(
                f"| `{verdict.target_id}` | {state} | "
                f"{verdict.reproduction_rate:.0%} "
                f"(95% CI {verdict.ci95[0]:.0%} to {verdict.ci95[1]:.0%}) | "
                f"c={verdict.concurrency}, {verdict.mode}, "
                f"{'amplified' if verdict.amplified else 'natural'} |"
            )
        for skipped in result.skipped:
            lines.append(f"| `{skipped.target_id}` | skipped | | {skipped.reason} |")
        lines.append("")

    if result.stale_baseline:
        lines.append(
            f"Baseline has {len(result.stale_baseline)} stale entr"
            f"{'y' if len(result.stale_baseline) == 1 else 'ies'}; regenerate it with "
            f"`raceguard baseline write`."
        )
        lines.append("")
    for notice in result.notices:
        lines.append(f"> {notice}")
    if result.notices:
        lines.append("")

    lines.append(
        "> A target with no violations was not reproduced under the stated seed, "
        "concurrency, mode and amplification. That is bounded evidence, not a "
        "proof of correctness."
    )
    return "\n".join(lines)


def render_text(result: CIResult) -> str:
    """Terminal output for the generic provider and local runs."""
    parts = [text_report.render_scan(result.scan, colour=False)]
    if result.gated:
        parts.append(f"\n{len(result.gated)} finding(s) gate this build (fail-on threshold met).")
    if result.out_of_diff:
        parts.append(f"{len(result.out_of_diff)} finding(s) outside the diff were not gated.")
    if result.baselined:
        parts.append(f"{len(result.baselined)} finding(s) are in the baseline and were not gated.")
    for verdict in result.verdicts:
        parts.append(f"verify {verdict.target_id}: {verdict.summary()}")
    for skipped in result.skipped:
        parts.append(f"verify {skipped.target_id}: skipped ({skipped.reason})")
    return "\n".join(parts)
