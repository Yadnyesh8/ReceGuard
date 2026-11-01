"""Regressions for the apply protocol and container-mode booting.

Every test here corresponds to a bug that actually shipped and was caught by
running the tool rather than by reading it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from raceguard.config import Config
from raceguard.contracts import Patch
from raceguard.errors import InfrastructureError, UsageError
from raceguard.harness.appboot import _reachable_from_container, build_app_image
from raceguard.patch import apply_patch, require_clean_tree

# -- container networking -------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "postgresql://u:p@localhost:55432/rg_trial_1",
            "postgresql://u:p@host.docker.internal:55432/rg_trial_1",
        ),
        (
            "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
            "postgresql+asyncpg://u:p@host.docker.internal:5432/db",
        ),
        ("postgresql://u:p@0.0.0.0:5432/db", "postgresql://u:p@host.docker.internal:5432/db"),
    ],
)
def test_loopback_is_rewritten_for_the_container(url, expected):
    """Inside a container `localhost` is the container itself, so an unrewritten
    URL connects to a database that is not there."""
    assert _reachable_from_container(url) == expected


def test_a_real_host_is_left_alone():
    url = "postgresql://u:p@db.internal:5432/app"
    assert _reachable_from_container(url) == url


def test_dockerfile_is_resolved_against_the_project(tmp_path):
    """`docker build -f` resolves against the CURRENT directory, not the build
    context, and RaceGuard is routinely run from a directory above the app."""
    with pytest.raises(InfrastructureError) as excinfo:
        build_app_image("Dockerfile", tmp_path, "runid")
    assert "not found at" in excinfo.value.message
    assert str(tmp_path) in excinfo.value.message


# -- the clean tree check -------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def test_clean_tree_accepts_a_committed_repo(tmp_path):
    require_clean_tree(_repo(tmp_path))


def test_clean_tree_rejects_real_changes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("x = 2\n")
    with pytest.raises(UsageError) as excinfo:
        require_clean_tree(repo)
    assert "app.py" in excinfo.value.message


def test_raceguard_artifacts_do_not_count_as_your_changes(tmp_path):
    """RaceGuard writes .raceguard/runs/ during the very command that then
    checks the tree. Counting that made --apply refuse because of something the
    tool itself did one second earlier."""
    repo = _repo(tmp_path)
    runs = repo / ".raceguard" / "runs" / "20260101-000000-abcd"
    runs.mkdir(parents=True)
    (runs / "command.txt").write_text("raceguard fix")
    require_clean_tree(repo)


def test_not_a_repo_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError) as excinfo:
        require_clean_tree(tmp_path)
    assert "not a git repository" in excinfo.value.message


# -- the restore guarantee ------------------------------------------------


def _patch(diff: str) -> Patch:
    return Patch(finding_id="x", strategy="atomic_update", diff=diff, rationale="r")


def _config() -> Config:
    return Config(app={"module": "m:app"}, targets=[])


DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_a_failing_test_suite_discards_the_change(tmp_path):
    """A patch that fixes a race and breaks four tests is worse than no patch."""
    repo = _repo(tmp_path)
    config = _config()
    config.app.test_command = "exit 1"

    result = apply_patch(_patch(DIFF), config, root=repo)
    assert not result.ok
    assert result.failed_step == "test_command"
    assert (repo / "app.py").read_text() == "x = 1\n", "the tree was not restored"


def test_an_exception_during_reverification_still_restores(tmp_path):
    """The worst outcome is an exception escaping with the patch still applied:
    the user is left holding a change they never accepted."""
    repo = _repo(tmp_path)

    def exploding_reverify(target, invariant):
        raise RuntimeError("the database went away")

    result = apply_patch(
        _patch(DIFF),
        _config(),
        target=object(),
        root=repo,
        reverify=exploding_reverify,
        run_tests=False,
    )
    assert not result.ok
    assert "the database went away" in result.output
    assert (repo / "app.py").read_text() == "x = 1\n", "the tree was not restored"


def test_a_passing_run_keeps_the_change(tmp_path):
    repo = _repo(tmp_path)
    config = _config()
    config.app.test_command = "exit 0"

    result = apply_patch(_patch(DIFF), config, root=repo, run_tests=True)
    assert result.ok
    assert result.patch is not None and result.patch.applied
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_an_unapplyable_diff_fails_before_touching_anything(tmp_path):
    repo = _repo(tmp_path)
    result = apply_patch(
        _patch("--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n"), _config(), root=repo
    )
    assert not result.ok
    assert result.failed_step == "apply"
    assert (repo / "app.py").read_text() == "x = 1\n"


def test_an_empty_diff_is_refused_early(tmp_path):
    result = apply_patch(_patch(""), _config(), root=tmp_path)
    assert not result.ok
    assert result.failed_step == "propose"


# -- the migration must reach the reverify database ------------------------


def test_a_migration_patch_without_a_reverifier_is_refused(tmp_path):
    """A schema-dependent patch (ON CONFLICT, a version predicate) must never be
    kept off a reverify that ran against a schema missing what it references.
    With no reverifier there is no honest way to test it, so it is refused
    rather than applied on faith."""
    from raceguard.contracts import Migration

    repo = _repo(tmp_path)
    patch = Patch(
        finding_id="x",
        strategy="insert_on_conflict",
        diff=DIFF,
        rationale="r",
        migration=Migration(
            kind="unique_index",
            statement='CREATE UNIQUE INDEX CONCURRENTLY "uq" ON "users" ("email");',
            alembic="op.create_index(...)",
        ),
    )
    result = apply_patch(patch, _config(), root=repo, run_tests=False)
    assert not result.ok
    assert result.failed_step == "migration"
    assert (repo / "app.py").read_text() == "x = 1\n", "tree must be restored"


def test_reverifier_applies_the_patch_migration_to_the_template():
    """The regression that made every insert_on_conflict / optimistic_version
    fix un-appliable: the reverify template was built from the ORIGINAL
    migrations, so the patched code referenced an index or column that did not
    exist and reverify always errored. The patch migration must be applied to
    the template before the trials clone from it."""
    from unittest.mock import MagicMock
    from unittest.mock import patch as mock_patch

    from raceguard.cli import _make_reverifier
    from raceguard.contracts import Migration

    cfg = MagicMock()
    cfg.database.template_name = "rg_template"
    migration = Migration(
        kind="unique_index",
        statement='CREATE UNIQUE INDEX CONCURRENTLY "uq" ON "users" ("email");',
        alembic="op.create_index(...)",
    )
    fake_ctx = MagicMock()
    fake_ctx.template_url = "postgresql://u@localhost:5432/rg_template"

    def _run(coro):
        # verify_target is a coroutine function, so the patched call returns an
        # awaitable; close it so it is not left un-awaited (a RuntimeWarning).
        if hasattr(coro, "close"):
            coro.close()
        return "verdict"

    with (
        mock_patch("raceguard.harness.start_run", return_value=fake_ctx),
        mock_patch("raceguard.harness.verify_target", return_value="verdict"),
        mock_patch("raceguard.db.apply_ddl") as apply_ddl,
        mock_patch("raceguard.synth.derive_for_target", return_value="inv"),
        mock_patch("raceguard.cli.asyncio.run", side_effect=_run),
    ):
        # Build the reverifier inside the patch context: it does a local
        # `from .harness import start_run`, which binds the name when the
        # factory runs, so it must run while the patch is active.
        reverify = _make_reverifier(cfg, migration=migration)
        reverify(MagicMock(), MagicMock())

    apply_ddl.assert_called_once()
    called_url, called_sql = apply_ddl.call_args.args
    assert called_url == fake_ctx.template_url
    assert "CREATE UNIQUE INDEX" in called_sql
    fake_ctx.cleanup.assert_called_once()  # template torn down even on the happy path


def test_a_discarded_patch_that_created_a_file_leaves_nothing_behind(tmp_path):
    """`git checkout -- .` reverts tracked files but not ones the diff created.
    Since require_clean_tree guarantees no untracked files going in, a discarded
    patch must also remove any file it added, or it leaves a stray behind."""
    repo = _repo(tmp_path)
    config = _config()
    config.app.test_command = "exit 1"  # force the discard
    creating = "--- /dev/null\n+++ b/new_helper.py\n@@ -0,0 +1,1 @@\n+STRAY = True\n"

    result = apply_patch(_patch(creating), config, root=repo)
    assert not result.ok
    assert not (repo / "new_helper.py").exists(), "a discarded patch left a stray file"
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert porcelain == "", f"tree not clean after restore: {porcelain!r}"


def test_test_command_resolves_tools_from_the_venv_without_activation(tmp_path, monkeypatch):
    """`fix --apply` ran test_command with the bare process PATH. Launched by
    absolute path (what `raceguard mcp install` writes, and what most CI does)
    the venv's scripts dir is not on it, so a correct `pytest -q` exited 127
    and the patch was discarded. migrate had the fix; test_command did not."""
    import os
    import stat
    import sysconfig

    scripts = tmp_path / "venv-scripts"
    scripts.mkdir()
    tool = scripts / "rg-probe-tests"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(sysconfig, "get_path", lambda name, *a, **k: str(scripts))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # an unactivated launch

    (tmp_path / "repo").mkdir()
    repo = _repo(tmp_path / "repo")
    config = _config()
    config.app.test_command = "rg-probe-tests"
    result = apply_patch(_patch(DIFF), config, root=repo, run_tests=True)
    assert result.ok, f"test_command did not resolve from the venv: {result.output}"
    assert os.environ["PATH"] == "/usr/bin:/bin", "the process PATH itself must be untouched"


def test_every_configured_command_gets_the_venv_on_path(monkeypatch):
    import sysconfig

    from raceguard.config import command_environment

    monkeypatch.setattr(sysconfig, "get_path", lambda name, *a, **k: "/venv/bin")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = command_environment({"DATABASE_URL": "postgresql://x"})
    assert env["PATH"].split(":")[0] == "/venv/bin"
    assert env["DATABASE_URL"] == "postgresql://x"


def test_a_missing_migrate_tool_points_at_installing_the_app():
    """The README quickstart skipped installing the example app, so every new
    user's first `verify` hit `alembic: command not found` and was told to
    "check app.migrate", a file that was already correct."""
    from raceguard.db import migrate_failure_hint

    hint = migrate_failure_hint("alembic upgrade head", 127)
    assert "'alembic' is not installed" in hint and "pip install -e ." in hint
    assert migrate_failure_hint("alembic upgrade head", 1) == "check app.migrate in raceguard.yaml"


def test_the_quickstart_installs_the_app_before_verifying():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    quickstart = readme.split("## Sixty seconds", 1)[1].split("```", 2)[1]
    steps = [line.split("#")[0].strip() for line in quickstart.splitlines() if line.strip()]
    assert "pip install -e ." in steps, "verify cannot migrate an app that was never installed"
    assert steps.index("pip install -e .") < steps.index("raceguard verify --target purchase")
