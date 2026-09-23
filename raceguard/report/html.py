"""Standalone HTML report: one file, no external assets, opens offline.

Deliberately plain. This is evidence a reviewer reads, not a dashboard, so it
carries the derivation of every invariant and the trial-by-trial record rather
than a headline number.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from ..contracts import Patch, ScanResult, Verdict

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
       margin: 0; padding: 2.5rem 1.5rem; background: #14120f; color: #ece7df; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.6rem; letter-spacing: -0.01em; margin: 0 0 0.35rem; }
h2 { font-size: 1.05rem; text-transform: none; margin: 2.5rem 0 0.75rem;
     padding-bottom: 0.4rem; border-bottom: 1px solid #35302a; }
.sub { color: #9d948a; margin: 0 0 2rem; }
.card { background: #1c1916; border: 1px solid #35302a; border-radius: 10px;
        padding: 1rem 1.15rem; margin-bottom: 0.9rem; }
.row { display: flex; gap: 0.75rem; align-items: baseline; flex-wrap: wrap; }
.tag { font-size: 0.72rem; letter-spacing: 0.06em; padding: 0.15rem 0.5rem;
       border-radius: 4px; border: 1px solid currentColor; }
.high { color: #e8836b; } .medium { color: #d8b45c; } .low { color: #8fa8c4; }
.violated { color: #e8836b; } .clean { color: #86b48b; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
pre { background: #100e0c; border: 1px solid #35302a; border-radius: 8px;
      padding: 0.8rem; overflow-x: auto; }
dl { display: grid; grid-template-columns: 8.5rem 1fr; gap: 0.3rem 1rem; margin: 0.6rem 0 0; }
dt { color: #9d948a; font-size: 0.85rem; }
dd { margin: 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; margin-top: 0.6rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #2a2621; }
th { color: #9d948a; font-weight: 500; }
.disclaimer { margin-top: 3rem; padding: 1rem 1.15rem; border-left: 3px solid #6f6559;
              background: #1c1916; color: #b8b0a5; font-size: 0.9rem; }
.diff .add { color: #86b48b; } .diff .del { color: #e8836b; } .diff .hunk { color: #8fa8c4; }
"""

DISCLAIMER = (
    "RaceGuard reports what it reproduced under stated conditions. One violating "
    "trial is a proof of the defect. Zero violating trials is evidence bounded by "
    "the trial count, the seed, the concurrency, the mode and the amplification "
    "setting: it is not a proof that the handler is correct."
)


def render(
    *,
    scan: ScanResult | None = None,
    verdicts: Iterable[Verdict] = (),
    patches: Iterable[Patch] = (),
    run_id: str = "",
    tool_version: str = "0.1.0",
) -> str:
    verdicts = list(verdicts)
    patches = list(patches)
    parts: list[str] = [
        "<!doctype html><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>RaceGuard report {html.escape(run_id)}</title>",
        f"<style>{_CSS}</style><main>",
        "<h1>RaceGuard report</h1>",
        f"<p class='sub'>run {html.escape(run_id) or '(unsaved)'} &middot; "
        f"raceguard {html.escape(tool_version)}</p>",
    ]

    if scan is not None:
        parts.append(f"<h2>Findings ({len(scan.findings)} in {scan.files_scanned} files)</h2>")
        if not scan.findings:
            parts.append(
                "<p class='sub'>Nothing matched the recognised patterns. That is not "
                "the same as the code being free of races.</p>"
            )
        for finding in scan.findings:
            parts.append(_finding_card(finding))

        if scan.hygiene:
            parts.append(f"<h2>Hygiene ({len(scan.hygiene)})</h2>")
            for item in scan.hygiene:
                parts.append(
                    f"<div class='card'><div class='row'>"
                    f"<span class='tag {item.severity}'>{html.escape(item.severity)}</span>"
                    f"<strong>{html.escape(item.rule)}</strong>"
                    f"<code>{html.escape(item.route)}</code></div>"
                    f"<p>{html.escape(item.explanation)}</p></div>"
                )

    if verdicts:
        parts.append(f"<h2>Verification ({len(verdicts)})</h2>")
        for verdict in verdicts:
            parts.append(_verdict_card(verdict))

    if patches:
        parts.append(f"<h2>Proposed changes ({len(patches)})</h2>")
        for patch in patches:
            parts.append(_patch_card(patch))

    parts.append(f"<p class='disclaimer'>{html.escape(DISCLAIMER)}</p>")
    parts.append("</main>")
    return "".join(parts)


def _finding_card(finding) -> str:
    return (
        f"<div class='card'><div class='row'>"
        f"<span class='tag {finding.confidence.value}'>{finding.confidence.value}</span>"
        f"<strong>{html.escape(finding.pattern)}</strong>"
        f"<code>{html.escape(finding.route or finding.handler)}</code></div>"
        f"<dl>"
        f"<dt>handler</dt><dd><code>{html.escape(finding.handler)}</code></dd>"
        f"<dt>read</dt><dd><code>{html.escape(finding.read_site.file)}:"
        f"{finding.read_site.start_line}</code></dd>"
        + (
            f"<dt>decision</dt><dd><code>line {finding.decision_site.start_line}</code></dd>"
            if finding.decision_site
            else ""
        )
        + f"<dt>write</dt><dd><code>line {finding.write_site.start_line}</code></dd>"
        + (
            f"<dt>tables</dt><dd>{html.escape(', '.join(finding.tables))}</dd>"
            if finding.tables
            else ""
        )
        + (
            f"<dt>partial guard</dt><dd>{html.escape(finding.guard_partial)}</dd>"
            if finding.guard_partial
            else ""
        )
        + f"<dt>why</dt><dd>{html.escape(finding.explanation)}</dd>"
        f"</dl>"
        + (
            f"<pre>{html.escape(finding.read_site.snippet)}</pre>"
            if finding.read_site.snippet
            else ""
        )
        + "</div>"
    )


def _verdict_card(verdict: Verdict) -> str:
    state = "violated" if verdict.violated else "clean"
    label = "VIOLATED" if verdict.violated else "not reproduced"
    rows = "".join(
        f"<tr><td>{t.index}</td><td><code>{html.escape(str(t.status_codes))}</code></td>"
        f"<td>{'yes' if t.violated else 'no'}</td>"
        f"<td>{t.arrival_spread_ms:.2f} ms</td>"
        f"<td>{html.escape((t.error or '')[:80])}</td></tr>"
        for t in verdict.trials[:40]
    )
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in verdict.notes)
    return (
        f"<div class='card'><div class='row'>"
        f"<span class='tag {state}'>{label}</span>"
        f"<strong>{html.escape(verdict.target_id)}</strong>"
        f"<code>{html.escape(verdict.invariant.kind)}</code></div>"
        f"<dl>"
        f"<dt>predicate</dt><dd><code>{html.escape(verdict.invariant.predicate)}</code></dd>"
        f"<dt>derivation</dt><dd>{html.escape(verdict.invariant.derivation)}</dd>"
        f"<dt>source</dt><dd>{html.escape(verdict.invariant.source)}</dd>"
        f"<dt>trials</dt><dd>{verdict.violating_trials} violated of "
        f"{verdict.usable_trials} usable ({verdict.total_trials} run)</dd>"
        f"<dt>rate</dt><dd>{verdict.reproduction_rate:.1%} "
        f"(95% CI {verdict.ci95[0]:.1%} to {verdict.ci95[1]:.1%})</dd>"
        f"<dt>conditions</dt><dd>concurrency {verdict.concurrency}, {verdict.mode} mode, "
        f"{verdict.sync} sync, "
        f"{'amplified ' + str(verdict.amplify_ms) + 'ms' if verdict.amplified else 'natural timing'}</dd>"
        f"</dl>"
        f"<pre>{html.escape(verdict.invariant.check_sql.strip())}</pre>"
        + (f"<ul>{notes}</ul>" if notes else "")
        + (
            f"<table><tr><th>trial</th><th>status codes</th><th>violated</th>"
            f"<th>arrival spread</th><th>error</th></tr>{rows}</table>"
            if rows
            else ""
        )
        + "</div>"
    )


def _patch_card(patch: Patch) -> str:
    diff_html = "".join(
        f"<span class='{_diff_class(line)}'>{html.escape(line)}</span>\n"
        for line in patch.diff.splitlines()
    )
    migration = ""
    if patch.migration:
        migration = (
            "<p><strong>This change is not sufficient on its own.</strong> "
            "Required migration:</p>"
            f"<pre>{html.escape(patch.migration.statement)}</pre>"
        )
    return (
        f"<div class='card'><div class='row'><strong>{html.escape(patch.strategy)}</strong>"
        f"<code>{html.escape(patch.finding_id)}</code></div>"
        f"<p>{html.escape(patch.rationale)}</p>"
        f"<pre class='diff'>{diff_html}</pre>"
        f"{migration}</div>"
    )


def _diff_class(line: str) -> str:
    if line.startswith("+") and not line.startswith("+++"):
        return "add"
    if line.startswith("-") and not line.startswith("---"):
        return "del"
    if line.startswith("@@"):
        return "hunk"
    return ""
