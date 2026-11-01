"""Defects found by running the tool the way a user would. Each test fails on
the code as it stood, and names what a user saw. No Docker."""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types
from pathlib import Path

import pytest
from test_ci import VULNERABLE, _git
from typer.testing import CliRunner

from raceguard import cli
from raceguard import harness as harness_module
from raceguard.ci import CIOptions, run
from raceguard.config import Config, Target
from raceguard.contracts import Invariant, TrialRecord, Verdict
from raceguard.errors import (
    EXIT_CONFIG,
    EXIT_FINDINGS,
    GovernorRefusal,
    TargetConfigError,
    UsageError,
)
from raceguard.harness import RunContext, appboot, start_run, verify_target
from raceguard.harness.amplify import amplified, pause, sitecustomize_source
from raceguard.harness.database import ContainerHandle
from raceguard.harness.governor import Governor
from raceguard.scanner import ScanOptions, scan

runner = CliRunner()
# `from raceguard.harness import fire` would get the function of that name.
fire = importlib.import_module("raceguard.harness.fire")


# -- verifying a real project ------------------------------------------------


def _fake_module(name: str, origin: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__spec__ = types.SimpleNamespace(origin=str(origin))
    return module


def test_reload_leaves_a_project_local_virtualenv_alone(tmp_path, monkeypatch):
    """Every trial reimported SQLAlchemy from .venv and failed with
    TypeError("'InternalTraversal' object is not callable")."""
    installed = tmp_path / ".venv/lib/python3.12/site-packages/somelib/__init__.py"
    own = tmp_path / "myapp_helpers.py"
    (tmp_path / "myapp_main.py").write_text("app = object()\n")
    for path in (installed, own):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    monkeypatch.setitem(sys.modules, "somelib", _fake_module("somelib", installed))
    monkeypatch.setitem(sys.modules, "myapp_helpers", _fake_module("myapp_helpers", own))
    keep = sys.modules["somelib"]
    monkeypatch.syspath_prepend(str(tmp_path))

    appboot._reload_app_modules("myapp_main:app", tmp_path)

    assert sys.modules["somelib"] is keep
    assert "myapp_helpers" not in sys.modules
    sys.modules.pop("myapp_main", None)


def test_reload_never_touches_raceguard_itself(tmp_path, monkeypatch):
    errors = sys.modules["raceguard.errors"]
    monkeypatch.setattr(
        errors, "__spec__", types.SimpleNamespace(origin=str(tmp_path / "x/errors.py"))
    )
    (tmp_path / "myapp_other.py").write_text("app = object()\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    appboot._reload_app_modules("myapp_other:app", tmp_path)
    assert sys.modules["raceguard.errors"] is errors
    sys.modules.pop("myapp_other", None)


def test_the_delay_lands_after_the_read_on_any_engine():
    """A sleep before the read does not widen read-to-write, and listening on
    one engine found by name missed every package not called shop, app or src."""
    from sqlalchemy import create_engine, event, text

    order: list[str] = []
    with amplified("widgets", 1) as state:
        engine = create_engine("sqlite://")  # built after the shim, found by nobody
        event.listen(engine, "before_cursor_execute", lambda *a: order.append("before"))
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE widgets (id int)"))
            event.listen(engine, "after_cursor_execute", lambda *a: order.append("after"))
            conn.execute(text("SELECT * FROM widgets"))
        assert state.hits == 1
    assert order == ["before", "before", "after"]


async def test_the_delay_does_not_freeze_an_async_app():
    """On an async engine the listener runs on the loop. time.sleep there stops
    the other request, which is the one the window exists to let in."""
    from sqlalchemy.util.concurrency import greenlet_spawn

    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(tick())
    await greenlet_spawn(pause, 0.05)
    task.cancel()
    assert ticks > 3


def test_the_delay_works_without_the_greenlet_library(monkeypatch):
    """A sync project often lacks it, and SQLAlchemy raises rather than returns."""
    import sqlalchemy.util.concurrency as concurrency

    def missing():
        raise ImportError("greenlet")

    monkeypatch.setattr(concurrency, "in_greenlet", missing)
    pause(0)


def test_the_container_shim_is_valid_python_and_hooks_after_the_read():
    source = sitecustomize_source("items", 25)
    compile(source, "sitecustomize.py", "exec")
    assert "after_cursor_execute" in source
    assert "before_cursor_execute" not in source


def test_the_image_pythonpath_survives_the_shim(monkeypatch):
    done = types.SimpleNamespace(returncode=0, stdout='["PATH=/bin", "PYTHONPATH=/app/src"]')
    monkeypatch.setattr(appboot.subprocess, "run", lambda *a, **k: done)
    assert appboot._image_pythonpath("img") == "/app/src"


async def test_single_packet_over_http_does_not_serialize(monkeypatch):
    """httpx speaks HTTP/1.1 over http://, and on its one connection the burst
    ran sequentially: no race, and a clean "not reproduced"."""

    async def fake_last_byte(base_url, requests, **kwargs):
        return fire.FireResult(sync="last_byte")

    monkeypatch.setattr(fire, "fire_last_byte", fake_last_byte)
    result = await fire.fire_single_packet("http://127.0.0.1:8000", [])
    assert result.sync == "last_byte"
    assert "single_packet was not possible" in result.notes[0]


# -- what a verdict is allowed to claim --------------------------------------


def _context(**governor) -> RunContext:
    config = Config.model_validate({"app": {"module": "a:b"}, "governor": governor})
    return RunContext(
        config=config,
        run_id="r",
        admin_url="postgresql://localhost/x",
        template_url="postgresql://localhost/t",
        governor=Governor(min_trial_spacing_ms=0),
        container=ContainerHandle(url="postgresql://localhost/x"),
    )


TARGET = Target(id="t", route="POST /x", amplify_table="items", mode="inprocess")
INVARIANT = Invariant(
    kind="non_negative",
    table="items",
    predicate="stock >= 0",
    check_sql="SELECT 1",
    derivation="test",
    source="config",
)


VERDICT = Verdict(
    finding_id="",
    target_id="VERIFIED_MARKER",
    invariant=INVARIANT,
    violated=True,
    violating_trials=1,
    total_trials=1,
    usable_trials=1,
    reproduction_rate=1.0,
    ci95=(0.2, 1.0),
    amplified=True,
    amplify_ms=25,
    sync="barrier",
    concurrency=2,
    mode="inprocess",
    duration_s=0.1,
)


def _fake_trials(monkeypatch, *, violated=True, hits=1):
    calls: list[dict] = []

    async def fake(context, target, invariant, **kwargs):
        calls.append(kwargs)
        kwargs["hits"].append(hits)
        return TrialRecord(
            index=kwargs["index"],
            concurrency=kwargs["concurrency"],
            status_codes=[200],
            violated=violated,
        )

    monkeypatch.setattr(harness_module, "run_trial", fake)
    return calls


async def test_amplified_phase_stops_early_but_the_natural_rate_does_not(monkeypatch):
    calls = _fake_trials(monkeypatch)
    await verify_target(_context(), TARGET, INVARIANT, trials=40, amplify=True)
    assert len(calls) == 10

    calls.clear()
    natural = await verify_target(_context(), TARGET, INVARIANT, trials=40, amplify=False)
    assert len(calls) == 40 and natural.total_trials == 40


async def test_a_delay_that_never_fired_is_not_reported_as_amplified(monkeypatch):
    _fake_trials(monkeypatch, violated=False, hits=0)
    verdict = await verify_target(_context(), TARGET, INVARIANT, trials=3, amplify=True)
    assert verdict.amplified is False
    assert any(n.startswith("WARNING") and "natural timing" in n for n in verdict.notes)


async def test_a_stopped_governor_ends_the_target_instead_of_erroring_every_trial(monkeypatch):
    calls = _fake_trials(monkeypatch)
    context = _context()
    context.governor.stopped_reason = "target returned 429"
    with pytest.raises(Exception, match="429"):
        await verify_target(context, TARGET, INVARIANT, trials=20, amplify=False)
    assert calls == []


async def test_the_run_budget_is_enforced(monkeypatch):
    calls = _fake_trials(monkeypatch)
    context = _context()
    context.started = time.monotonic() - context.config.defaults.run_budget_s - 1
    with pytest.raises(Exception, match="run budget"):
        await verify_target(context, TARGET, INVARIANT, trials=20, amplify=False)
    assert calls == []


async def test_an_external_target_caps_the_burst_itself(monkeypatch):
    calls = _fake_trials(monkeypatch)
    context = _context(allow_external_target=True)
    await verify_target(context, TARGET, INVARIANT, trials=1, concurrency=30)
    assert calls[0]["concurrency"] == 4


async def test_inprocess_refuses_a_sync_that_needs_a_socket():
    target = TARGET.model_copy(update={"sync": "last_byte"})
    with pytest.raises(TargetConfigError, match="inprocess"):
        await verify_target(_context(), target, INVARIANT, trials=1)


# -- the ownership check -----------------------------------------------------


def _start(monkeypatch, url, *, external, allow=False, confirmed=None):
    handle = ContainerHandle(url=url, external=external)
    monkeypatch.setattr(harness_module, "start_postgres", lambda *a, **k: handle)
    monkeypatch.setattr(harness_module.db, "create_template", lambda *a, **k: url)
    monkeypatch.setattr(harness_module, "_install_signal_handlers", lambda c: None)
    config = Config.model_validate(
        {"app": {"module": "a:b"}, "governor": {"allow_external_target": allow}}
    )
    return start_run(config, confirmed_host=confirmed)


def test_confirming_an_external_target_means_typing_the_right_host(monkeypatch):
    """The value typed was never compared; the check was handed the host it was
    checking, so any string, or a line of config, unlocked production."""
    url = "postgresql://prod.example.com/app"
    with pytest.raises(GovernorRefusal):
        _start(monkeypatch, url, external=True, allow=True, confirmed=None)
    with pytest.raises(GovernorRefusal):
        _start(monkeypatch, url, external=True, allow=True, confirmed="yes")
    assert _start(monkeypatch, url, external=True, allow=True, confirmed="prod.example.com")


def test_our_own_container_is_ours_on_any_host(monkeypatch):
    """Docker-in-Docker publishes on `docker`, not loopback, and every run there
    was refused for firing at a database it had just created."""
    assert _start(monkeypatch, "postgresql://docker:32768/test", external=False)


def test_a_cli_refusal_uses_the_value_typed(monkeypatch, shop):
    seen: dict = {}

    def fake(config, *, run_id=None, confirmed_host=None):
        seen["host"] = confirmed_host
        raise GovernorRefusal("stop")

    monkeypatch.setattr(harness_module, "start_run", fake)
    monkeypatch.chdir(shop)
    runner.invoke(cli.app, ["verify", "--allow-external-target", "typed.example"])
    assert seen["host"] == "typed.example"


# -- the command line --------------------------------------------------------


def test_a_failed_target_keeps_the_verdicts_already_earned(monkeypatch, shop, tmp_path):
    """One bad target at the end of a long run discarded every earlier result."""
    from raceguard import synth

    calls = 0

    async def fake(context, target, invariant, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TargetConfigError("second target failed")
        return VERDICT, None

    context = _context()
    monkeypatch.setattr(harness_module, "start_run", lambda *a, **k: context)
    monkeypatch.setattr(harness_module, "verify_two_phase", fake)
    monkeypatch.setattr(synth, "derive_for_target", lambda *a, **k: INVARIANT)
    monkeypatch.setattr(RunContext, "cleanup", lambda self: None)
    monkeypatch.chdir(shop)
    output = tmp_path / "out.txt"
    with pytest.raises(TargetConfigError):
        runner.invoke(cli.app, ["verify", "-o", str(output)], catch_exceptions=False)
    assert "VERIFIED_MARKER" in output.read_text()


def test_scan_takes_the_many_filenames_pre_commit_passes(shop, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    routes = shop / "shop/routes"
    result = runner.invoke(
        cli.app, ["scan", "--no-cache", str(routes / "orders.py"), str(routes / "wallet.py")]
    )
    assert "unexpected extra argument" not in result.output
    assert result.exit_code == EXIT_FINDINGS
    assert "orders.py" in result.output and "wallet.py" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["scan", "does-not-exist"],
        ["scan", ".", "--fail-on", "bogus"],
        ["scan", ".", "--min-confidence", "bogus"],
        ["scan", ".", "--format", "bogus"],
        ["verify", "--format", "bogus"],
        ["baseline", "check", ".", "--fail-on", "bogus"],
    ],
)
def test_a_usage_mistake_is_exit_2_not_a_finding(arguments, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(UsageError) as caught:
        result = runner.invoke(cli.app, arguments, catch_exceptions=False)
        assert result.exit_code == EXIT_CONFIG
    assert caught.value.exit_code == EXIT_CONFIG


def test_a_rejected_patch_is_not_a_success(shop, monkeypatch):
    from raceguard import patch as patch_module

    monkeypatch.setattr(
        patch_module,
        "apply_patch",
        lambda *a, **k: patch_module.ApplyResult(ok=False, failed_step="reverify", output="no"),
    )
    monkeypatch.chdir(shop)
    result = runner.invoke(cli.app, ["fix", "--target", "purchase", "--apply"])
    assert result.exit_code == EXIT_FINDINGS


# -- CI ----------------------------------------------------------------------


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    for service in ("shop", "billing"):
        (tmp_path / "services" / service).mkdir(parents=True)
        (tmp_path / "services" / service / "orders.py").write_text(VULNERABLE)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "feature")
    return tmp_path


def _ci(path: Path, **kwargs) -> CIOptions:
    return CIOptions(
        path=str(path), provider="generic", sarif=None, hygiene=False, annotations=False, **kwargs
    )


def test_a_monorepo_service_gates_on_its_own_diff(monorepo):
    """git printed paths from the repository root and findings are relative to
    the service, so nothing ever matched and every race passed."""
    edited = monorepo / "services/shop/orders.py"
    edited.write_text(edited.read_text() + "\n# touched\n")
    _git(monorepo, "commit", "-qam", "edit")

    result = run(_ci(monorepo / "services/shop", base="main", changed_only=True))
    assert [f.read_site.file for f in result.gated] == ["orders.py"]
    assert result.exit_code == EXIT_FINDINGS


def test_a_change_elsewhere_in_the_monorepo_gates_nothing_here(monorepo):
    edited = monorepo / "services/billing/orders.py"
    edited.write_text(edited.read_text() + "\n# touched\n")
    _git(monorepo, "commit", "-qam", "edit")

    result = run(_ci(monorepo / "services/shop", base="main", changed_only=True))
    assert result.gated == [] and result.changed_only is True


def test_ci_honours_suppressions_in_the_config(tmp_path):
    """They worked in `scan` and nowhere else, so an accepted finding still
    failed the build."""
    (tmp_path / "orders.py").write_text(VULNERABLE)
    (tmp_path / "raceguard.yaml").write_text(
        "version: 1\napp: {module: a:b}\n"
        "suppress:\n  - {rule: oversell, file: orders.py, reason: accepted}\n"
    )
    result = run(_ci(tmp_path, changed_only=False))
    assert result.gated == []


def test_a_group_target_matches_its_findings_by_route():
    from raceguard.ci import targets_in_diff
    from raceguard.contracts import CodeSpan, Confidence, Finding

    target = Target(id="t", group=[{"route": "POST /a"}, {"route": "POST /b"}])
    span = CodeSpan(file="a.py", start_line=1, end_line=1)
    finding = Finding(
        id="f",
        handler="h",
        route="POST /b",
        pattern="oversell",
        read_site=span,
        write_site=span,
        confidence=Confidence.high,
        explanation="",
    )
    config = Config.model_validate({"app": {"module": "a:b"}})
    config.targets = [target]
    assert targets_in_diff(config, [finding], {"a.py"}) == ["t"]


def test_ci_refuses_a_path_that_is_not_there(tmp_path):
    with pytest.raises(UsageError):
        run(_ci(tmp_path / "typo"))


def test_ci_refuses_a_bad_threshold(tmp_path):
    with pytest.raises(UsageError):
        run(_ci(tmp_path, fail_on="bogus"))


# -- the scan cache ----------------------------------------------------------


MAIN = """\
from fastapi import FastAPI
from orders import router
app = FastAPI()
app.include_router(router, prefix="{prefix}")
"""


def test_a_cached_route_follows_its_include_prefix(tmp_path):
    """The prefix lives in another file, so the handler file hashes the same."""
    (tmp_path / "orders.py").write_text(VULNERABLE)
    options = ScanOptions(cache_dir=tmp_path / "cache", root=tmp_path)

    (tmp_path / "main.py").write_text(MAIN.format(prefix="/v1"))
    assert scan([str(tmp_path)], options).findings[0].route == "POST /v1/orders"

    (tmp_path / "main.py").write_text(MAIN.format(prefix="/v2"))
    assert scan([str(tmp_path)], options).findings[0].route == "POST /v2/orders"


def test_the_cache_does_not_outlive_the_tool_version(tmp_path, monkeypatch):
    """A rule fix must reach people who already have a cache."""
    import raceguard.scanner as scanner

    cache = scanner.ParseCache(tmp_path)
    before = cache._key("source", Path("a.py"))
    monkeypatch.setattr(scanner, "TOOL_VERSION", "99.0.0")
    assert cache._key("source", Path("a.py")) != before


# -- MCP ---------------------------------------------------------------------


def test_the_mcp_extra_only_allows_sdk_v2():
    """The v1 branch crashed at startup: FastMCP has no `version` argument."""
    import tomllib

    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["mcp"] == ["mcp>=2.1,<3"]


def test_get_run_does_not_follow_a_symlink_out_of_the_jail(tmp_path):
    secret = tmp_path / "outside.json"
    secret.write_text('{"token": "s3cret"}')
    root = tmp_path / "project"
    run_dir = root / ".raceguard/runs/20260101-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "verdicts.json").write_text("[]")
    (run_dir / "leak.json").symlink_to(secret)

    from raceguard.mcp.server import build
    from raceguard.mcp.settings import MCPSettings

    server, _ = build(MCPSettings(project_root=root))

    async def call():
        tool = next(t for t in await server.list_tools() if t.name == "get_run")
        assert tool
        return await server.call_tool("get_run", {"run_id": "abc"})

    assert "s3cret" not in str(asyncio.run(call()))
