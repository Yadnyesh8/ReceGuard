"""The command line adapter.

Exit codes matter and are never conflated. A CI system that cannot tell a
broken Docker daemon from a security finding will eventually treat both as
noise:

    0  completed, nothing above the configured gate
    1  findings or violations above the gate
    2  configuration or usage error
    3  infrastructure failure
    4  interrupted

Every run writes ``.raceguard/runs/<timestamp>-<id>/`` holding the resolved
config, findings, invariants with their derivations, verdicts, trial records,
final state dumps, diffs, and a log, with secrets redacted at write time. Bug
reports become "attach the run folder".
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from . import __version__ as TOOL_VERSION
from .config import Config, Target, find_config, load_config
from .contracts import Confidence, ScanResult, Verdict
from .errors import (
    EXIT_FINDINGS,
    EXIT_INFRA,
    EXIT_INTERRUPTED,
    EXIT_OK,
    RaceGuardError,
)
from .report import html as html_report
from .report import json as json_report
from .report import sarif as sarif_report
from .report import text as text_report

app = typer.Typer(
    name="raceguard",
    help=(
        "Find, prove, and repair atomicity violations in Python HTTP handlers.\n\n"
        "RaceGuard never says a handler is safe. It says what it found statically, "
        "what it reproduced dynamically, and under exactly which conditions."
    ),
    no_args_is_help=True,
    add_completion=False,
)
console = Console(stderr=True)
out = Console()

RUNS_DIR = Path(".raceguard/runs")
CACHE_DIR = Path(".raceguard/cache")


# --------------------------------------------------------------------------
# Run artifacts
# --------------------------------------------------------------------------


@dataclass
class Run:
    id: str
    directory: Path | None
    started: float

    @classmethod
    def start(cls, root: Path, command: str) -> Run:
        """Open a run folder, or carry on without one.

        Run artifacts are how a bug report becomes "attach this folder", but a
        scan is a read-only operation. Refusing to run because the working
        directory is not writable, which is exactly the case inside the tool's
        own container with the source mounted read-only, would be the wrong
        trade.
        """
        run_id = uuid.uuid4().hex[:8]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        directory: Path | None = root / RUNS_DIR / f"{stamp}-{run_id}"
        try:
            assert directory is not None
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "command.txt").write_text(command, encoding="utf-8")
        except OSError as exc:
            console.print(
                f"[dim]not recording run artifacts: {root / RUNS_DIR} is not "
                f"writable ({exc.strerror}). The run itself is unaffected.[/]"
            )
            directory = None
        return cls(id=run_id, directory=directory, started=time.monotonic())

    def write(self, name: str, content: str) -> None:
        if self.directory is None:
            return
        try:
            (self.directory / name).write_text(content, encoding="utf-8")
        except OSError:  # pragma: no cover - artifacts are best effort
            pass

    def write_json(self, name: str, payload) -> None:
        self.write(name, jsonlib.dumps(payload, indent=2, default=str))


def _configure_logging(verbose: bool, run_id: str) -> None:
    """Structured JSON to stderr with the run id on every line."""
    level = logging.DEBUG if verbose else logging.INFO

    class _Formatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return jsonlib.dumps(
                {
                    "run": run_id,
                    "level": record.levelname.lower(),
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    root = logging.getLogger("raceguard")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def _load(path: str | None, start: str = ".") -> Config:
    return load_config(path, start=start)


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


@app.command()
def scan(
    path: str = typer.Argument(".", help="File or directory to scan."),
    format: str = typer.Option("text", "--format", "-f", help="text | json | sarif | html"),
    min_confidence: str = typer.Option("medium", "--min-confidence", help="high | medium | low"),
    show_suppressed: bool = typer.Option(False, "--show-suppressed"),
    hygiene: bool = typer.Option(False, "--hygiene", help="Also run the hygiene rules."),
    fail_on: str = typer.Option("high", "--fail-on", help="Gate: high | medium | low | never"),
    config: str | None = typer.Option(None, "--config", "-c"),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to a file."),
    no_cache: bool = typer.Option(False, "--no-cache"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Static scan for read-modify-write windows. No Docker required."""
    from .scanner import ScanOptions
    from .scanner import scan as run_scan
    from .scanner.suppress import ConfigSuppression

    root = Path(path if Path(path).is_dir() else Path(path).parent).resolve()
    run = Run.start(Path.cwd(), f"raceguard scan {path}")
    _configure_logging(verbose, run.id)

    suppressions: list[ConfigSuppression] = []
    loaded: Config | None = None
    try:
        if config or find_config(path):
            loaded = _load(config, start=path)
            suppressions = [
                ConfigSuppression(rule=s.rule, reason=s.reason, handler=s.handler, file=s.file)
                for s in loaded.suppress
            ]
            root = loaded.root.resolve()
    except RaceGuardError as exc:
        console.print(f"[yellow]config not usable, scanning without it: {exc.message}[/]")

    options = ScanOptions(
        min_confidence=Confidence(min_confidence),
        show_suppressed=show_suppressed,
        config_suppressions=suppressions,
        cache_dir=None if no_cache else _writable_cache(),
        root=root,
    )
    result = run_scan([path], options)

    if hygiene:
        from .hygiene import check as hygiene_check

        rules = loaded.hygiene.rules if loaded and loaded.hygiene.enabled else None
        result.hygiene = hygiene_check([path], rules=rules, root=root)

    rendered = _render_scan(result, format, run.id)
    _emit(rendered, output)

    run.write_json("findings.json", result.model_dump(mode="json"))
    if loaded is not None:
        run.write("config.resolved.json", jsonlib.dumps(loaded.redacted(), indent=2))

    raise typer.Exit(_gate(result, fail_on))


def _writable_cache() -> Path | None:
    """The parse cache is an optimisation. A read-only tree simply does without."""
    cache = Path.cwd() / CACHE_DIR
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return cache


def _render_scan(result: ScanResult, format: str, run_id: str) -> str:
    if format == "json":
        return json_report.render(scan=result, tool_version=TOOL_VERSION, run_id=run_id)
    if format == "sarif":
        return sarif_report.render(result, tool_version=TOOL_VERSION, category="scan")
    if format == "html":
        return html_report.render(scan=result, run_id=run_id, tool_version=TOOL_VERSION)
    rendered = text_report.render_scan(result, colour=out.is_terminal)
    return rendered + "\n" + text_report.render_footer(out.is_terminal)


def _gate(result: ScanResult, fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    threshold = Confidence(fail_on).rank()
    above = [f for f in result.findings if f.confidence.rank() >= threshold]
    high_hygiene = [h for h in result.hygiene if h.severity == "high"]
    return EXIT_FINDINGS if (above or high_hygiene) else EXIT_OK


def _emit(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"[dim]written to {output}[/]")
    else:
        # soft_wrap: the renderers already wrapped to a fixed width. Letting
        # Rich re-wrap breaks words at the terminal edge and mangles diffs.
        out.print(text, highlight=False, markup=False, soft_wrap=True)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@app.command()
def verify(
    target: list[str] = typer.Option(None, "--target", "-t", help="Target id; repeatable."),
    trials: int | None = typer.Option(None, "--trials"),
    concurrency: int | None = typer.Option(None, "--concurrency"),
    mode: str | None = typer.Option(None, "--mode", help="container | inprocess"),
    no_amplify: bool = typer.Option(False, "--no-amplify", help="Natural timing only."),
    format: str = typer.Option("text", "--format", "-f"),
    config: str | None = typer.Option(None, "--config", "-c"),
    output: str | None = typer.Option(None, "--output", "-o"),
    allow_external_target: str | None = typer.Option(
        None,
        "--allow-external-target",
        help="Type the host name to confirm firing at something RaceGuard did not create.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Boot an ephemeral PostgreSQL, fire concurrent requests, check the invariant."""
    from .harness import start_run, verify_two_phase
    from .synth import derive_for_target

    cfg = _load(config)
    run = Run.start(cfg.root, "raceguard verify")
    _configure_logging(verbose, run.id)

    if mode:
        cfg.defaults.mode = mode  # type: ignore[assignment]
    if allow_external_target:
        cfg.governor.allow_external_target = True
        console.print(
            "[yellow]external target confirmed: the concurrency ceiling drops to "
            f"{cfg.governor.external_concurrency_ceiling} and amplification is "
            "disabled.[/]"
        )
    if cfg.defaults.mode == "inprocess":
        console.print(f"[yellow]{_INPROCESS_WARNING}[/]")

    targets = [cfg.target(t) for t in target] if target else cfg.targets
    if not targets:
        raise RaceGuardError(
            "no targets defined", hint="run `raceguard init` to generate them from a scan"
        )

    context = start_run(cfg)
    verdicts: list[Verdict] = []
    rendered_parts: list[str] = []
    try:
        for entry in targets:
            invariant = derive_for_target(
                cfg, entry, admin_url=context.admin_url, template=cfg.database.template_name
            )
            run.write_json(f"invariant.{entry.id}.json", invariant.model_dump(mode="json"))

            amplified, natural = asyncio.run(
                verify_two_phase(
                    context,
                    entry,
                    invariant,
                    trials=trials,
                    concurrency=concurrency,
                    **({"amplify": False} if no_amplify else {}),
                )
            )
            verdicts.append(amplified)
            if natural is not None:
                verdicts.append(natural)
            rendered_parts.append(
                text_report.render_two_phase(amplified, natural, colour=out.is_terminal)
            )
        run.write_json("governor.json", context.governor.stats.as_dict())
    finally:
        context.cleanup()

    if format == "json":
        rendered = json_report.render(verdicts=verdicts, tool_version=TOOL_VERSION, run_id=run.id)
    elif format == "html":
        rendered = html_report.render(verdicts=verdicts, run_id=run.id, tool_version=TOOL_VERSION)
    else:
        rendered = "\n".join(rendered_parts) + "\n" + text_report.render_footer(out.is_terminal)
    _emit(rendered, output)

    run.write_json("verdicts.json", [v.model_dump(mode="json") for v in verdicts])
    run.write("config.resolved.json", jsonlib.dumps(cfg.redacted(), indent=2))
    raise typer.Exit(EXIT_FINDINGS if any(v.violated for v in verdicts) else EXIT_OK)


_INPROCESS_WARNING = (
    "inprocess mode imports the application under test into this process. It is "
    "strictly less safe than container mode; use container mode for any "
    "repository you do not control."
)


# --------------------------------------------------------------------------
# fix
# --------------------------------------------------------------------------


@app.command()
def fix(
    target: str | None = typer.Option(None, "--target", "-t"),
    finding: str | None = typer.Option(None, "--finding", help="Finding id from a scan."),
    rule: str | None = typer.Option(
        None, "--rule", help="A hygiene rule, e.g. missing_rate_limit."
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply, reverify, and run your tests."),
    path: str = typer.Option(".", "--path"),
    config: str | None = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Propose a change. Prints a diff and exits unless --apply is given."""
    from .patch import apply_patch, propose
    from .scanner import ScanOptions
    from .scanner import scan as run_scan
    from .synth import from_config

    cfg = _load(config)
    run = Run.start(cfg.root, "raceguard fix")
    _configure_logging(verbose, run.id)

    if rule:
        _fix_hygiene(rule, cfg, run, apply=apply)
        raise typer.Exit(EXIT_OK)

    result = run_scan([str(cfg.root)], ScanOptions(min_confidence=Confidence.medium, root=cfg.root))
    findings = result.findings
    if finding:
        findings = [f for f in findings if f.id == finding]
    elif target:
        entry = cfg.target(target)
        route = entry.route
        findings = [f for f in findings if f.route == route]
    if not findings:
        console.print("[yellow]nothing to fix: no matching finding above medium confidence[/]")
        raise typer.Exit(EXIT_OK)

    invariants = {t.invariant.table: from_config(t) for t in cfg.targets if t.invariant}
    patches = []
    for item in findings:
        invariant = next((invariants.get(t) for t in item.tables if t in invariants), None)
        patch = propose(item, invariant, root=cfg.root)
        patches.append(patch)
        out.print(
            text_report.render_patch(patch, colour=out.is_terminal),
            highlight=False,
            markup=False,
            soft_wrap=True,
        )
        out.print("")

        if apply:
            # Step 4 of the apply protocol is reverification. Applying without
            # it would mean claiming a fix that was never re-tested, which is
            # the one thing this tool must not do.
            matched: Target | None = _target_for_finding(cfg, item)
            invariant = invariants.get(item.tables[0]) if item.tables else None
            reverify = None
            if matched is not None:
                # The patch's own migration is applied to the reverify template
                # so the patched code is tested against the schema it requires.
                reverify = _make_reverifier(cfg, migration=patch.migration)
            elif not target:
                console.print(
                    "[yellow]no target in raceguard.yaml matches this finding, so the "
                    "patch cannot be reverified. Applying it would mean claiming a fix "
                    "that was never re-tested; add a target and try again.[/]"
                )
                continue

            result_apply = apply_patch(
                patch,
                cfg,
                target=matched,
                invariant=invariant,
                root=cfg.root,
                reverify=reverify,
            )
            if result_apply.ok:
                console.print(
                    "[green]applied: the race did not reproduce after the patch and "
                    "your test suite passed[/]"
                )
                if patch.migration is not None:
                    console.print(
                        "[yellow]the code change alone is NOT sufficient. Apply this "
                        "migration too:[/]"
                    )
                    out.print(patch.migration.statement, soft_wrap=True)
            else:
                console.print(
                    f"[red]not applied: step '{result_apply.failed_step}' failed, so "
                    f"the change was discarded[/]\n{result_apply.output[:2000]}"
                )
    run.write_json("patches.json", [p.model_dump(mode="json") for p in patches])
    if not apply:
        console.print(
            "[dim]nothing was written. Re-run with --apply to apply, reverify, and test.[/]"
        )
    raise typer.Exit(EXIT_OK)


def _target_for_finding(cfg: Config, finding) -> Target | None:
    for entry in cfg.targets:
        if entry.route and entry.route == finding.route:
            return entry
    return None


def _make_reverifier(cfg: Config, migration=None):
    """Step 4: expect zero violations in T of T trials, amplified.

    When the patch ships a schema change, that change is applied to the
    template BEFORE the trials clone from it. Without this the patched code
    (an ON CONFLICT against a unique index, a version-column predicate) runs
    against a schema that lacks what it references, so the reverify errors and
    an otherwise-correct fix can never be applied.
    """
    from . import db
    from .harness import start_run, verify_target
    from .synth import derive_for_target

    def reverify(target, invariant):
        context = start_run(cfg)
        try:
            if migration is not None:
                db.apply_ddl(context.template_url, migration.statement)
            resolved = invariant or derive_for_target(
                cfg,
                target,
                admin_url=context.admin_url,
                template=cfg.database.template_name,
            )
            return asyncio.run(verify_target(context, target, resolved, amplify=True))
        finally:
            context.cleanup()

    return reverify


def _fix_hygiene(rule: str, cfg: Config, run: Run, *, apply: bool) -> None:
    if rule != "missing_rate_limit":
        raise RaceGuardError(
            f"no generator for hygiene rule {rule!r}",
            hint="only missing_rate_limit can currently be generated",
        )
    from .hygiene.limiter import LimiterSpec, generate

    name, source = generate(LimiterSpec())
    destination = cfg.root / Path(cfg.app.source_root) / name
    if apply:
        destination.write_text(source, encoding="utf-8")
        console.print(f"[green]written {destination}[/]")
        console.print("[dim]wire it up with app.add_middleware(RateLimitMiddleware)[/]")
    else:
        out.print(source, highlight=False, markup=False, soft_wrap=True)
        console.print(f"[dim]would write {destination}; re-run with --apply[/]")
    run.write(name, source)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


@app.command()
def init(
    path: str = typer.Argument(".", help="Project root to scan."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raceguard.yaml."),
) -> None:
    """Generate raceguard.yaml from a scan. Nobody hand writes that file."""
    from .init import generate_config

    root = Path(path).resolve()
    destination = root / "raceguard.yaml"
    if destination.exists() and not force:
        raise RaceGuardError(f"{destination} already exists", hint="pass --force to overwrite it")
    text = generate_config(root)
    destination.write_text(text, encoding="utf-8")
    out.print(f"wrote {destination}")
    out.print(
        "[dim]Review it: auth headers, payload values, and any invariant marked as "
        "a suggestion need a human decision.[/]"
    )
    raise typer.Exit(EXIT_OK)


# --------------------------------------------------------------------------
# report, clean, doctor
# --------------------------------------------------------------------------


@app.command()
def report(
    run: str = typer.Option("latest", "--run", help="Run id, or 'latest'."),
    format: str = typer.Option("text", "--format", "-f", help="text | html | json"),
    output: str | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Re-render a stored run."""
    root = Path.cwd()
    runs = sorted((root / RUNS_DIR).glob("*")) if (root / RUNS_DIR).is_dir() else []
    if not runs:
        raise RaceGuardError(
            "no runs found", hint="run `raceguard scan` or `raceguard verify` first"
        )
    directory = (
        runs[-1] if run == "latest" else next((d for d in runs if d.name.endswith(run)), None)
    )
    if directory is None:
        raise RaceGuardError(f"no run matching {run!r}")

    scan_result = _read_model(directory / "findings.json", ScanResult)
    verdicts = [Verdict.model_validate(v) for v in _read_json(directory / "verdicts.json", [])]
    if format == "html":
        rendered = html_report.render(
            scan=scan_result, verdicts=verdicts, run_id=directory.name, tool_version=TOOL_VERSION
        )
    elif format == "json":
        rendered = json_report.render(
            scan=scan_result, verdicts=verdicts, tool_version=TOOL_VERSION, run_id=directory.name
        )
    else:
        parts = []
        if scan_result:
            parts.append(text_report.render_scan(scan_result, colour=out.is_terminal))
        parts += [text_report.render_verdict(v, colour=out.is_terminal) for v in verdicts]
        parts.append(text_report.render_footer(out.is_terminal))
        rendered = "\n".join(parts)
    _emit(rendered, output)
    raise typer.Exit(EXIT_OK)


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return jsonlib.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _read_model(path: Path, model):
    payload = _read_json(path, None)
    return model.model_validate(payload) if payload else None


@app.command()
def clean(
    all_runs: bool = typer.Option(False, "--runs", help="Also delete stored run folders."),
    force: bool = typer.Option(
        False, "--force", help="Remove containers even if a run appears to be in progress."
    ),
) -> None:
    """Remove containers and trial databases left behind by a killed run."""
    removed = 0
    # `clean` matches on the raceguard.run label, which every run's containers
    # carry, including one that is happening right now in another terminal.
    # Killing a live run's database mid-trial produces a confusing connection
    # error in that other window, so say so before doing it.
    if not force:
        live = subprocess.run(
            ["docker", "ps", "-q", "--filter", "label=raceguard.run"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        running = [line for line in live.stdout.split() if line]
        if running:
            console.print(
                f"[yellow]{len(running)} raceguard container(s) are currently "
                f"RUNNING. If a verification is in progress in another terminal, "
                f"removing them will abort it. Re-run with --force to proceed.[/]"
            )
            raise typer.Exit(EXIT_OK)
    try:
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=raceguard.run"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        ids = [line for line in listed.stdout.split() if line]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
            removed = len(ids)
        subprocess.run(
            ["docker", "network", "prune", "-f", "--filter", "label=raceguard.run"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"[yellow]docker cleanup skipped: {exc}[/]")

    out.print(f"removed {removed} container(s) labelled raceguard.run")
    cache = Path.cwd() / CACHE_DIR
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
        out.print("cleared the scan cache")
    if all_runs:
        runs = Path.cwd() / RUNS_DIR
        if runs.is_dir():
            shutil.rmtree(runs, ignore_errors=True)
            out.print("deleted stored run folders")
    raise typer.Exit(EXIT_OK)


@app.command()
def doctor(
    config: str | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Check Docker, image pull, ports, permissions, and the config."""
    checks: list[tuple[str, bool, str]] = []

    checks.append(("python", sys.version_info >= (3, 11), sys.version.split()[0]))

    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
        checks.append(
            (
                "docker daemon",
                info.returncode == 0,
                "reachable" if info.returncode == 0 else info.stderr.strip()[:120],
            )
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(("docker daemon", False, str(exc)[:120]))

    for module, extra in (
        ("psycopg", "harness"),
        ("testcontainers", "harness"),
        ("libcst", "core"),
    ):
        try:
            __import__(module)
            checks.append((f"import {module}", True, extra))
        except ImportError:
            checks.append((f"import {module}", False, f"pip install 'raceguard[{extra}]'"))

    try:
        cfg = _load(config)
        checks.append(("raceguard.yaml", True, f"{len(cfg.targets)} target(s)"))
        checks.append(
            (
                "database image pinned by digest",
                cfg.database.pinned_by_digest,
                cfg.database.image
                if cfg.database.pinned_by_digest
                else "pinned by tag; a moved tag can silently change results",
            )
        )
        if cfg.app.module:
            checks.append(("app.module set (inprocess mode)", True, cfg.app.module))
        if cfg.defaults.concurrency > cfg.app.thread_pool_size:
            checks.append(
                (
                    "concurrency vs thread pool",
                    False,
                    f"concurrency {cfg.defaults.concurrency} exceeds the declared pool of "
                    f"{cfg.app.thread_pool_size}; `def` handlers will queue and hide a race",
                )
            )
    except RaceGuardError as exc:
        checks.append(("raceguard.yaml", False, exc.message))

    failed = 0
    for name, ok, detail in checks:
        mark = "[green]ok[/]" if ok else "[red]FAIL[/]"
        failed += not ok
        out.print(f"  {mark:20} {name:38} {detail}")
    raise typer.Exit(EXIT_OK if failed == 0 else EXIT_INFRA)


@app.command()
def version() -> None:
    """Print the version."""
    out.print(f"raceguard {TOOL_VERSION}")


# --------------------------------------------------------------------------
# ci: the one entry point every CI system calls
# --------------------------------------------------------------------------


@app.command()
def ci(
    path: str = typer.Argument(".", help="Project root."),
    provider: str = typer.Option("auto", "--provider", help="auto | github | gitlab | generic"),
    base: str | None = typer.Option(
        None, "--base", help="Diff base ref; auto-detected per provider."
    ),
    changed_only: bool | None = typer.Option(
        None,
        "--changed-only/--all-files",
        help="Gate on findings in changed files only (default: on for pull requests).",
    ),
    fail_on: str = typer.Option("high", "--fail-on", help="high | medium | low | never"),
    baseline: str | None = typer.Option(
        None, "--baseline", help="Accepted findings, by fingerprint."
    ),
    verify: bool = typer.Option(
        False, "--verify/--no-verify", help="Verify targets whose handler changed."
    ),
    verify_required: bool = typer.Option(
        False, "--verify-required", help="Fail (exit 3) rather than skip when Docker is missing."
    ),
    budget_seconds: int | None = typer.Option(
        None, "--budget-seconds", help="Wall clock cap for verification."
    ),
    hygiene: bool = typer.Option(True, "--hygiene/--no-hygiene"),
    sarif: str | None = typer.Option(
        "raceguard.sarif", "--sarif", help="SARIF 2.1.0 output; '' to skip."
    ),
    json_path: str | None = typer.Option(None, "--json", help="Machine readable output path."),
    codequality: str | None = typer.Option(
        None, "--codequality", help="GitLab Code Quality report path."
    ),
    summary_path: str | None = typer.Option(None, "--summary", help="Markdown summary path."),
    annotations: bool = typer.Option(True, "--annotations/--no-annotations"),
    comment: bool = typer.Option(False, "--comment/--no-comment", help="Sticky PR/MR comment."),
    category: str = typer.Option("raceguard", "--category", help="SARIF category."),
) -> None:
    """Scan, verify what changed, gate, and emit every report the CI host reads.

    Exit 0 clean, 1 gated findings or violations, 2 configuration, 3 infrastructure.
    """
    from .ci import CIOptions, render_text
    from .ci import run as run_ci

    options = CIOptions(
        path=path,
        provider=provider,
        base=base,
        changed_only=changed_only,
        fail_on=fail_on,
        baseline=baseline,
        verify=verify,
        verify_required=verify_required,
        budget_seconds=budget_seconds,
        hygiene=hygiene,
        sarif=sarif or None,
        json_path=json_path,
        codequality=codequality,
        summary_path=summary_path,
        annotations=annotations,
        comment=comment,
        category=category,
    )
    result = run_ci(options)
    out.print(render_text(result), highlight=False, markup=False, soft_wrap=True)
    for name, written in result.written.items():
        console.print(f"[dim]{name}: {written}[/]")
    raise typer.Exit(result.exit_code)


baseline_app = typer.Typer(help="Accepted findings a build should report but not block on.")
app.add_typer(baseline_app, name="baseline")


@baseline_app.command("write")
def baseline_write(
    path: str = typer.Argument(".", help="Project root."),
    output: str = typer.Option(".raceguard/baseline.json", "--output", "-o"),
) -> None:
    """Record every current finding's fingerprint so the next run does not gate on it."""
    from .ci.baseline import Baseline
    from .scanner import ScanOptions
    from .scanner import scan as run_scan

    root = Path(path).resolve()
    result = run_scan([str(root)], ScanOptions(min_confidence=Confidence.low, root=root))
    written = Baseline.from_findings(result.findings).write(root / output)
    out.print(f"wrote {written} with {len(result.findings)} fingerprint(s)")
    console.print(
        "[dim]A baseline is accepted debt, not a fix. Regenerate it as findings are "
        "resolved so it shrinks rather than rots.[/]"
    )


@baseline_app.command("check")
def baseline_check(
    path: str = typer.Argument(".", help="Project root."),
    baseline: str = typer.Option(".raceguard/baseline.json", "--baseline", "-b"),
    fail_on: str = typer.Option("high", "--fail-on"),
) -> None:
    """Exit 1 on findings above the gate that are not in the baseline; report stale entries."""
    from .ci.baseline import Baseline
    from .scanner import ScanOptions
    from .scanner import scan as run_scan

    root = Path(path).resolve()
    accepted = Baseline.load(root / baseline)
    result = run_scan([str(root)], ScanOptions(min_confidence=Confidence.low, root=root))
    threshold = Confidence(fail_on).rank() if fail_on != "never" else 99
    new = [
        f
        for f in result.findings
        if not f.suppressed and f.confidence.rank() >= threshold and not accepted.contains(f)
    ]
    stale = accepted.stale(result.findings)
    for finding in new:
        out.print(
            f"new: {finding.read_site.file}:{finding.read_site.start_line} "
            f"{finding.pattern} [{finding.confidence.value}] {finding.route or ''}"
        )
    if stale:
        console.print(
            f"[yellow]{len(stale)} stale baseline entr{'y' if len(stale) == 1 else 'ies'}; regenerate with `raceguard baseline write`[/]"
        )
    if new:
        out.print(f"{len(new)} finding(s) not in the baseline")
        raise typer.Exit(EXIT_FINDINGS)
    out.print(f"no findings above {fail_on} outside the baseline")


# --------------------------------------------------------------------------
# mcp: serve and install
# --------------------------------------------------------------------------


mcp_app = typer.Typer(help="The MCP server: stdio for editors, HTTP for hosting.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    transport: str | None = typer.Option(None, "--transport", help="stdio | http"),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    project_root: str | None = typer.Option(
        None, "--project-root", help="Sandbox root for every path."
    ),
    allow_any_path: bool = typer.Option(
        False, "--allow-any-path", help="Disable the path sandbox (stdio only)."
    ),
    allow_anonymous: bool = typer.Option(
        False, "--allow-anonymous", help="HTTP without bearer tokens (loopback only)."
    ),
    no_verify: bool = typer.Option(False, "--no-verify", help="Disable verify_handler entirely."),
) -> None:
    """Start the MCP server. Settings come from RACEGUARD_MCP_* with these overrides."""
    from .mcp.server import serve
    from .mcp.settings import MCPSettings

    settings = MCPSettings.from_env()
    if transport:
        settings.transport = transport  # type: ignore[assignment]
    if host:
        settings.host = host
    if port:
        settings.port = port
    if project_root:
        settings.project_root = Path(project_root).resolve()
    if allow_any_path:
        settings.allow_any_path = True
    if allow_anonymous:
        settings.allow_anonymous = True
    if no_verify:
        settings.verify_enabled = False
    serve(settings)


@mcp_app.command("install")
def mcp_install(
    host: str = typer.Argument(..., help="claude-desktop | claude-code | cursor | vscode"),
    write: bool = typer.Option(False, "--write", help="Write into the host's config file."),
    project_root: str | None = typer.Option(None, "--project-root"),
) -> None:
    """Print (or write) the host configuration that launches this server over stdio."""
    from .mcp.install import install

    rendered, written = install(host, write=write, project_root=project_root)
    out.print(rendered, highlight=False, markup=False, soft_wrap=True)
    if written:
        console.print(f"[green]written to {written}[/]")


# --------------------------------------------------------------------------


def main() -> None:
    try:
        app()
    except RaceGuardError as exc:
        console.print(f"[red]error:[/] {exc.render()}")
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        console.print("[yellow]interrupted; cleaning up[/]")
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":
    main()
