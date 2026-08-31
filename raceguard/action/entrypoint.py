"""GitHub Action entrypoint.

Workflow shape (section 14.3): scan on every push, and verify only the targets
whose handler files appear in the diff. Verifying everything on every push will
outlast the rest of the pipeline, and a check that takes longer than the build
gets switched off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from ..config import find_config, load_config
from ..contracts import Confidence, ScanResult, Verdict
from ..errors import EXIT_FINDINGS, EXIT_INFRA, EXIT_OK, RaceGuardError
from ..report import sarif as sarif_report
from ..report import text as text_report


def changed_files(base_ref: str | None) -> set[str]:
    """Files touched by this push or pull request."""
    if not base_ref:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "diff", "--name-only", base_ref],
                capture_output=True, text=True, timeout=60,
            )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def targets_in_diff(config, findings: Iterable, touched: set[str]) -> list[str]:
    """Only the targets whose handler file actually changed."""
    if not touched:
        return []
    changed_routes = {
        f.route for f in findings if f.read_site.file in touched and f.route
    }
    return [t.id for t in config.targets if t.route in changed_routes]


def summary(scan: ScanResult, verdicts: list[Verdict]) -> str:
    """GitHub step summary, in Markdown."""
    lines = ["## RaceGuard", ""]
    counts: dict[str, int] = {}
    for finding in scan.findings:
        counts[finding.confidence.value] = counts.get(finding.confidence.value, 0) + 1
    if counts:
        lines.append(
            "Findings: " + ", ".join(f"**{v}** {k}" for k, v in sorted(counts.items()))
        )
    else:
        lines.append("No read-modify-write windows matched the recognised patterns.")
    lines.append("")

    if scan.findings:
        lines += ["| confidence | pattern | route | location |", "|---|---|---|---|"]
        for finding in scan.findings[:25]:
            lines.append(
                f"| {finding.confidence.value} | `{finding.pattern}` | "
                f"`{finding.route or '?'}` | "
                f"`{finding.read_site.file}:{finding.read_site.start_line}` |"
            )
        lines.append("")

    if verdicts:
        lines += ["### Verification", "", "| target | result | rate | conditions |", "|---|---|---|---|"]
        for verdict in verdicts:
            state = "**violated**" if verdict.violated else "not reproduced"
            lines.append(
                f"| `{verdict.target_id}` | {state} | "
                f"{verdict.reproduction_rate:.0%} "
                f"(CI {verdict.ci95[0]:.0%}–{verdict.ci95[1]:.0%}) | "
                f"c={verdict.concurrency}, {verdict.mode}, "
                f"{'amplified' if verdict.amplified else 'natural'} |"
            )
        lines.append("")

    lines.append(
        "> A target with no violations was not reproduced under the stated seed, "
        "concurrency, mode and amplification. That is bounded evidence, not a "
        "proof of correctness."
    )
    return "\n".join(lines)


def main() -> int:
    path = os.environ.get("INPUT_PATH", ".")
    sarif_path = os.environ.get("INPUT_SARIF_FILE", "raceguard.sarif")
    fail_on = os.environ.get("INPUT_FAIL_ON", "high")
    do_verify = os.environ.get("INPUT_VERIFY", "false").lower() == "true"
    include_hygiene = os.environ.get("INPUT_HYGIENE", "true").lower() == "true"
    base_ref = os.environ.get("GITHUB_BASE_REF") or os.environ.get("INPUT_BASE_REF")

    from ..scanner import ScanOptions
    from ..scanner import scan as run_scan

    root = Path(path).resolve()
    result = run_scan(
        [path], ScanOptions(min_confidence=Confidence.low, root=root)
    )
    if include_hygiene:
        from ..hygiene import check

        result.hygiene = check([path], root=root)

    verdicts: list[Verdict] = []
    exit_code = EXIT_OK

    if do_verify:
        config_path = find_config(path)
        if config_path is None:
            print("::warning::verify requested but no raceguard.yaml was found", file=sys.stderr)
        else:
            try:
                verdicts = _verify_changed(config_path, result, base_ref)
            except RaceGuardError as exc:
                print(f"::error::{exc.message}", file=sys.stderr)
                exit_code = EXIT_INFRA

    # SARIF must stay under 10 MB, and each results file in one workflow needs a
    # distinct category or a unique runAutomationDetails.id.
    document = sarif_report.build(
        result, verdicts=verdicts, category=os.environ.get("INPUT_CATEGORY", "raceguard")
    )
    Path(sarif_path).write_text(json.dumps(document, indent=2), encoding="utf-8")
    size_mb = Path(sarif_path).stat().st_size / (1024 * 1024)
    if size_mb > 10:
        print(
            f"::warning::{sarif_path} is {size_mb:.1f} MB; GitHub rejects SARIF over 10 MB",
            file=sys.stderr,
        )

    print(text_report.render_scan(result, colour=False))

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary(result, verdicts) + "\n")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"findings={len(result.findings)}\n")
            handle.write(f"violations={sum(1 for v in verdicts if v.violated)}\n")
            handle.write(f"sarif={sarif_path}\n")

    if fail_on != "never":
        threshold = Confidence(fail_on).rank()
        gated = [f for f in result.findings if f.confidence.rank() >= threshold]
        if gated or any(v.violated for v in verdicts):
            exit_code = EXIT_FINDINGS
    return exit_code


def _verify_changed(config_path: Path, result: ScanResult, base_ref: str | None) -> list[Verdict]:
    import asyncio

    from ..harness import start_run, verify_target
    from ..synth import derive_for_target

    config = load_config(str(config_path))
    touched = changed_files(base_ref)
    ids = targets_in_diff(config, result.findings, touched)
    if not ids:
        print("no changed handler maps to a configured target; skipping verification")
        return []

    print(f"verifying {len(ids)} target(s) whose handler changed: {', '.join(ids)}")
    context = start_run(config)
    verdicts: list[Verdict] = []
    try:
        for target_id in ids:
            target = config.target(target_id)
            invariant = derive_for_target(
                config, target, admin_url=context.admin_url,
                template=config.database.template_name,
            )
            verdicts.append(
                asyncio.run(verify_target(context, target, invariant, amplify=True))
            )
    finally:
        context.cleanup()
    return verdicts


if __name__ == "__main__":
    sys.exit(main())
