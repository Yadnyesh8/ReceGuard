"""Terminal rendering.

One rule governs every string in this package: the tool never says "safe",
"secure", or "verified". A tool that overclaims once loses the room
permanently. ``tests/test_report_language.py`` asserts those words never appear
in rendered output, so this is enforced rather than remembered.
"""

from __future__ import annotations

from ..contracts import (
    Confidence,
    Finding,
    HygieneFinding,
    Patch,
    ScanResult,
    Verdict,
)

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GREEN = "\033[32m"

_CONFIDENCE_COLOR = {
    Confidence.high: RED,
    Confidence.medium: YELLOW,
    Confidence.low: BLUE,
}

FOOTER = (
    "RaceGuard reports what it reproduced under the stated conditions. "
    "A target with no violations is not proven correct: it was not reproduced "
    "with this seed, this concurrency, this mode and this amplification setting."
)


class Palette:
    def __init__(self, colour: bool = True) -> None:
        self.colour = colour

    def __call__(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text


def render_scan(result: ScanResult, *, colour: bool = True, show_suppressed: bool = False) -> str:
    c = Palette(colour)
    lines: list[str] = []

    if not result.findings and not result.hygiene:
        lines.append(
            f"No read-modify-write windows found in {result.files_scanned} file(s) "
            f"({result.duration_s:.2f}s)."
        )
        lines.append(
            c("  This means nothing matched the recognised patterns, not that the "
              "code is free of races.", DIM)
        )
    else:
        lines.append(
            c(f"{len(result.findings)} finding(s) in {result.files_scanned} file(s) "
              f"({result.duration_s:.2f}s)", BOLD)
        )
        lines.append("")

    for finding in result.findings:
        lines.extend(_render_finding(finding, c))

    if result.hygiene:
        lines.append(c(f"Hygiene ({len(result.hygiene)}):", BOLD))
        for item in result.hygiene:
            lines.extend(_render_hygiene(item, c))

    if show_suppressed and result.suppressed:
        lines.append(c(f"Suppressed ({len(result.suppressed)}):", BOLD))
        for finding in result.suppressed:
            lines.append(
                f"  {finding.read_site.file}:{finding.read_site.start_line} "
                f"{finding.pattern} - {finding.suppression_reason}"
            )
        lines.append("")

    if result.errors:
        lines.append(c(f"Problems ({len(result.errors)}):", YELLOW))
        for error in result.errors:
            lines.append(f"  {error}")
        lines.append("")

    return "\n".join(lines)


def _render_finding(finding: Finding, c: Palette) -> list[str]:
    colour = _CONFIDENCE_COLOR.get(finding.confidence, BLUE)
    header = (
        f"{finding.read_site.file}:{finding.read_site.start_line} "
        f"{c(finding.pattern, colour)} "
        f"[{finding.confidence.value}]"
    )
    lines = [header]
    if finding.route:
        lines.append(f"  route      {finding.route}")
    lines.append(f"  handler    {finding.handler}")
    lines.append(f"  read       line {finding.read_site.start_line}")
    if finding.decision_site:
        lines.append(f"  decision   line {finding.decision_site.start_line}")
    lines.append(f"  write      line {finding.write_site.start_line}")
    if finding.tables:
        lines.append(f"  tables     {', '.join(finding.tables)}")
    if finding.guard_partial:
        lines.append(c(f"  partial    {finding.guard_partial}", YELLOW))
    lines.append(f"  why        {_wrap(finding.explanation, 13)}")
    lines.append("")
    return lines


def _render_hygiene(item: HygieneFinding, c: Palette) -> list[str]:
    colour = {"high": RED, "medium": YELLOW, "low": BLUE}[item.severity]
    return [
        f"  {c(item.rule, colour)} [{item.severity}] {item.route}",
        f"    {_wrap(item.explanation, 4)}",
        "",
    ]


def render_verdict(verdict: Verdict, *, colour: bool = True) -> str:
    c = Palette(colour)
    lines: list[str] = []
    status = (
        c("VIOLATED", RED) if verdict.violated else c("not reproduced", GREEN)
    )
    lines.append(c(f"target {verdict.target_id}", BOLD) + f"  {status}")
    lines.append(f"  invariant   {verdict.invariant.kind}: {verdict.invariant.predicate}")
    lines.append(f"  derivation  {_wrap(verdict.invariant.derivation, 14)}")
    lines.append(f"  source      {verdict.invariant.source}")
    if verdict.invariant.critic_note:
        lines.append(f"  critic      {_wrap(verdict.invariant.critic_note, 14)}")
    lines.append(f"  check_sql   {verdict.invariant.check_sql.strip().splitlines()[0]}")
    lines.append(
        f"  trials      {verdict.violating_trials} violated of "
        f"{verdict.usable_trials} usable ({verdict.total_trials} run)"
    )
    lines.append(
        f"  rate        {verdict.reproduction_rate:.1%}  "
        f"95% CI {verdict.ci95[0]:.1%} to {verdict.ci95[1]:.1%}"
    )
    lines.append(
        f"  conditions  concurrency {verdict.concurrency}, {verdict.mode} mode, "
        f"{verdict.sync} sync, "
        + (f"amplified {verdict.amplify_ms}ms" if verdict.amplified else "natural timing")
    )
    lines.append(f"  duration    {verdict.duration_s:.1f}s")
    for note in verdict.notes:
        prefix = c("  ! ", YELLOW) if note.startswith("WARNING") else "  - "
        lines.append(prefix + _wrap(note, 4))
    lines.append("")
    return "\n".join(lines)


def render_two_phase(
    amplified: Verdict, natural: Verdict | None, *, colour: bool = True
) -> str:
    """Amplified and natural are reported separately, never merged.

    The amplified number is the structural claim: this window exists and can be
    entered. The natural number is the operational claim: this is how often it
    fired unaided. One blended figure would be a different, and false, claim.
    """
    c = Palette(colour)
    parts = [c("amplified phase (does the window exist?)", BOLD), render_verdict(amplified, colour=colour)]
    if natural is not None:
        parts.append(c("natural phase (how often does it fire unaided?)", BOLD))
        parts.append(render_verdict(natural, colour=colour))
    else:
        parts.append(
            c("natural phase skipped: the amplified phase found no violation, so "
              "there is no window to measure the unaided rate of.", DIM)
        )
    return "\n".join(parts)


def render_patch(patch: Patch, *, colour: bool = True) -> str:
    c = Palette(colour)
    lines = [c(f"strategy: {patch.strategy}", BOLD), ""]
    lines.append(_wrap(patch.rationale, 0))
    lines.append("")
    if patch.diff:
        lines.append(_colour_diff(patch.diff, c))
    else:
        lines.append(c("no diff proposed for this finding.", YELLOW))
    if patch.migration:
        lines.append("")
        lines.append(c("This change is NOT sufficient on its own. Required migration:", YELLOW))
        lines.append(patch.migration.statement)
        lines.append("")
        lines.append(c("Alembic equivalent:", DIM))
        lines.append(patch.migration.alembic)
    for note in patch.notes:
        lines.append(c(f"  note: {_wrap(note, 8)}", DIM))
    if patch.reverified is not None:
        lines.append("")
        lines.append(render_verdict(patch.reverified, colour=colour))
    if patch.test_suite_passed is not None:
        state = "passed" if patch.test_suite_passed else "FAILED"
        lines.append(f"your test suite: {state}")
    return "\n".join(lines)


def _colour_diff(diff: str, c: Palette) -> str:
    out = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(c(line, BOLD))
        elif line.startswith("+"):
            out.append(c(line, GREEN))
        elif line.startswith("-"):
            out.append(c(line, RED))
        elif line.startswith("@@"):
            out.append(c(line, BLUE))
        else:
            out.append(line)
    return "\n".join(out)


def _wrap(text: str, indent: int, width: int = 88) -> str:
    import textwrap

    wrapped = textwrap.wrap(text or "", width=width - indent)
    if not wrapped:
        return ""
    pad = " " * indent
    return ("\n" + pad).join(wrapped)


def render_footer(colour: bool = True) -> str:
    return Palette(colour)(FOOTER, DIM)
