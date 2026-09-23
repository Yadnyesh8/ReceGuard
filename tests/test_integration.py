"""Full pipeline against a real PostgreSQL container.

This is the test that matters. It proves the loop the whole project claims:
find a window statically, reproduce it dynamically against a real database,
generate a patch, and show the patch closes it.

Marked `docker` and skipped without a daemon, because a suite that cannot run
without Docker is a suite people stop running.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import requires_docker

from raceguard.config import load_config
from raceguard.contracts import Confidence
from raceguard.harness import start_run, verify_target, verify_two_phase
from raceguard.patch import propose
from raceguard.scanner import ScanOptions, scan
from raceguard.synth import derive_for_target, reflect

pytestmark = [pytest.mark.docker, pytest.mark.slow, requires_docker]


@pytest.fixture(scope="module")
def context(shop):
    config = load_config(str(shop / "raceguard.yaml"))
    config.defaults.mode = "inprocess"
    run = start_run(config)
    try:
        yield config, run
    finally:
        run.cleanup()


def test_template_is_migrated_and_reflectable(context):
    _config, run = context
    schema = reflect(run.template_url)
    assert "items" in schema.tables
    assert "stock" in schema.tables["items"].columns


def test_template_has_no_open_sessions(context):
    """CREATE DATABASE ... TEMPLATE fails if anything is still connected, and
    the error names the template rather than the migration that left the
    connection open. This is the single most common bug in the harness."""
    from raceguard.db import connect

    _config, run = context
    with connect(run.admin_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            ("rg_template",),
        )
    assert rows[0][0] == 0


def test_trial_databases_are_created_and_dropped(context):
    from raceguard.db import connect, create_trial_database, drop_trial_database

    _config, run = context
    name, url = create_trial_database(run.admin_url, 999)
    try:
        with connect(url) as conn:
            assert conn.execute("SELECT 1 FROM items LIMIT 1") is not None
    finally:
        drop_trial_database(run.admin_url, name)
    with connect(run.admin_url, autocommit=True) as conn:
        remaining = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    assert not remaining


def test_invariant_passes_the_four_step_gate(context):
    config, run = context
    target = config.target("purchase")
    invariant = derive_for_target(config, target, admin_url=run.admin_url)
    assert invariant.source == "config"
    assert invariant.check_sql.strip()


def test_oversell_reproduces(context):
    """The headline claim: a real race, proven against a real database."""
    config, run = context
    target = config.target("purchase")
    invariant = derive_for_target(config, target, admin_url=run.admin_url)
    verdict = asyncio.run(verify_target(run, target, invariant, trials=6, amplify=True))

    assert verdict.violated, verdict.notes
    assert verdict.violating_trials >= 1
    assert verdict.usable_trials >= 1
    # Two buyers, one unit of stock, both accepted: that is the oversell.
    assert any(t.successes == 2 for t in verdict.trials if t.usable)


def test_verdict_records_its_conditions(context):
    config, run = context
    target = config.target("purchase")
    invariant = derive_for_target(config, target, admin_url=run.admin_url)
    verdict = asyncio.run(verify_target(run, target, invariant, trials=3, amplify=True))
    assert verdict.mode == "inprocess"
    assert verdict.amplified and verdict.amplify_ms > 0
    assert verdict.concurrency == 2
    assert any("amplified" in note for note in verdict.notes)


def test_two_phase_reports_both_numbers_separately(context):
    """The amplified number is the structural claim, the natural number the
    operational one. Merging them would be a different, false claim."""
    config, run = context
    target = config.target("purchase")
    invariant = derive_for_target(config, target, admin_url=run.admin_url)
    amplified, natural = asyncio.run(verify_two_phase(run, target, invariant, trials=4))
    assert amplified.amplified
    assert natural is not None
    assert not natural.amplified
    assert amplified.amplify_ms > 0 and natural.amplify_ms == 0


def test_governor_actually_engaged(context):
    _config, run = context
    stats = run.governor.stats
    assert stats.requests_fired > 0
    assert stats.trials_run > 0
    # The inter-trial spacing is enforced, so a run is bursts and not a flood.
    assert stats.pauses > 0


def test_patch_closes_the_race(shop):
    """find -> prove -> repair -> re-prove, end to end."""
    scratch = Path(tempfile.mkdtemp(prefix="raceguard-int-"))
    work = scratch / "shop"
    shutil.copytree(shop, work)
    try:
        result = scan([str(shop / "shop")], ScanOptions(min_confidence=Confidence.high, root=shop))
        finding = next(f for f in result.findings if f.route == "POST /orders")
        patch = propose(finding, None, root=shop)
        assert patch.strategy == "atomic_update"
        assert patch.diff.strip()

        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        applied = subprocess.run(
            ["git", "apply", "-"], cwd=work, input=patch.diff, capture_output=True, text=True
        )
        assert applied.returncode == 0, applied.stderr

        config = load_config(str(work / "raceguard.yaml"))
        config.defaults.mode = "inprocess"
        sys.path.insert(0, str(work))
        run = start_run(config)
        try:
            target = config.target("purchase")
            invariant = derive_for_target(config, target, admin_url=run.admin_url)
            verdict = asyncio.run(verify_target(run, target, invariant, trials=8, amplify=True))
            assert not verdict.violated, f"the patch did not close the race: {verdict.summary()}"
            # Exactly one buyer wins every time now.
            for trial in verdict.trials:
                if trial.usable:
                    assert trial.successes == 1, trial.status_codes
        finally:
            run.cleanup()
            if str(work) in sys.path:
                sys.path.remove(str(work))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
