"""Remediation: the strategy table, the codemods, and the refusal to emit
a patch that would not compile."""

from __future__ import annotations

import ast
import builtins
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from raceguard.contracts import CodeSpan, Confidence, Finding
from raceguard.patch import propose
from raceguard.patch.migrations import unique_index, version_column
from raceguard.patch.strategies import choose


def finding(pattern, *, snippet="if item.stock < qty:", partial=None, file="a.py") -> Finding:
    span = CodeSpan(file=file, start_line=1, end_line=1, snippet=snippet)
    return Finding(
        id="x",
        handler="m:h",
        pattern=pattern,
        read_site=span,
        write_site=span,
        decision_site=span,
        confidence=Confidence.high,
        explanation="",
        guard_partial=partial,
    )


# -- the decision table ---------------------------------------------------


@pytest.mark.parametrize(
    "pattern,snippet,partial,expected",
    [
        ("oversell", "if item.stock < qty:", None, "atomic_update"),
        ("single_use", "if c.redeemed:", None, "atomic_update"),
        ("duplicate_insert", "if existing is None:", None, "insert_on_conflict"),
        ("aggregate_limit", "if count >= 5:", None, "advisory_lock"),
        ("lost_update", "x = 1", None, "optimistic_version"),
        (
            "oversell",
            "if item.stock < qty:",
            "serializable isolation, no retry wrapper",
            "serializable_retry",
        ),
    ],
)
def test_strategy_selection(pattern, snippet, partial, expected):
    assert choose(finding(pattern, snippet=snippet, partial=partial)).strategy == expected


def test_decision_that_cannot_move_into_sql_takes_a_lock_instead():
    """Pushing a predicate the database cannot evaluate into a WHERE clause
    would change behaviour, not fix a race."""
    decision = choose(finding("oversell", snippet="if await pricing_engine(item):"))
    assert decision.strategy == "select_for_update"


def test_strategies_needing_schema_changes_say_so():
    assert choose(finding("duplicate_insert", snippet="if x is None:")).needs_migration
    assert choose(finding("lost_update", snippet="x = 1")).needs_migration
    assert not choose(finding("oversell")).needs_migration


# -- migrations -----------------------------------------------------------


def test_unique_index_is_concurrent_and_idempotent():
    migration = unique_index("users", ["email"])
    assert "CONCURRENTLY" in migration.statement
    assert "IF NOT EXISTS" in migration.statement
    assert "autocommit_block" in migration.alembic


def test_check_constraint_migration_avoids_a_long_lock():
    from raceguard.patch.migrations import check_constraint

    migration = check_constraint("items", "stock")
    assert "NOT VALID" in migration.statement
    assert "VALIDATE CONSTRAINT" in migration.statement


def test_version_column_has_a_default_for_existing_rows():
    assert "DEFAULT 1" in version_column("docs").statement


# -- the codemods, against the real reference app -------------------------


@pytest.fixture(scope="module")
def shop_patches(shop, shop_scan):
    from raceguard.config import load_config
    from raceguard.synth import from_config

    config = load_config(str(shop / "raceguard.yaml"))
    invariants = {t.invariant.table: from_config(t) for t in config.targets if t.invariant}
    out = []
    for item in shop_scan.findings:
        invariant = next((invariants.get(t) for t in item.tables if t in invariants), None)
        out.append((item, propose(item, invariant, root=shop)))
    return out


def test_every_patch_applies_and_parses(shop, shop_patches):
    """A patch that does not compile is worse than no patch."""
    for item, patch in shop_patches:
        if not patch.diff.strip():
            continue
        scratch = Path(tempfile.mkdtemp())
        try:
            copy = scratch / "app"
            shutil.copytree(shop, copy)
            applied = subprocess.run(
                ["git", "apply", "-"],
                cwd=copy,
                input=patch.diff,
                capture_output=True,
                text=True,
            )
            assert applied.returncode == 0, f"{item.route}: {applied.stderr}"
            source = (copy / item.read_site.file).read_text()
            ast.parse(source)  # raises SyntaxError on a broken patch
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def test_patched_code_defines_every_name_it_uses(shop, shop_patches):
    """The liveness guard: removing the read must not strand a name the
    response still refers to."""
    for item, patch in shop_patches:
        if not patch.diff.strip():
            continue
        scratch = Path(tempfile.mkdtemp())
        try:
            copy = scratch / "app"
            shutil.copytree(shop, copy)
            subprocess.run(
                ["git", "apply", "-"], cwd=copy, input=patch.diff, capture_output=True, text=True
            )
            tree = ast.parse((copy / item.read_site.file).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                bound = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                        bound.add(inner.id)
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        bound |= {a.asname or a.name.split(".")[0] for a in inner.names}
                used_locals = {
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                }
                module_level = (
                    {
                        n.id
                        for n in ast.walk(tree)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                    }
                    | set(dir(builtins))
                    | {
                        a.asname or a.name.split(".")[0]
                        for s in ast.walk(tree)
                        if isinstance(s, (ast.Import, ast.ImportFrom))
                        for a in s.names
                    }
                )
                dangling = used_locals - bound - module_level
                # Anything genuinely global is filtered above; what is left
                # would raise NameError at runtime.
                assert not dangling or all(d[0].isupper() for d in dangling), (
                    f"{item.route} ({patch.strategy}): {sorted(dangling)} undefined"
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def test_oversell_becomes_a_conditional_update(shop_patches):
    item, patch = next(
        (f, p)
        for f, p in shop_patches
        if f.route == "POST /orders" and p.strategy == "atomic_update"
    )
    assert "update(Item)" in patch.diff
    assert "Item.stock >= payload.qty" in patch.diff
    assert "result.rowcount == 0" in patch.diff


def test_duplicate_insert_ships_its_migration(shop_patches):
    item, patch = next((f, p) for f, p in shop_patches if f.pattern == "duplicate_insert")
    assert patch.migration is not None
    assert "CREATE UNIQUE INDEX" in patch.migration.statement
    assert any("does NOTHING without the unique index" in n for n in patch.notes)


def test_multi_row_lock_orders_by_primary_key(shop_patches):
    """Locking rows in inconsistent order trades a race for a deadlock."""
    matches = [p for _, p in shop_patches if p.strategy == "select_for_update" and p.diff]
    assert matches
    assert any("order_by" in p.diff for p in matches)


def _run_codemod(command_cls, target, source):
    import libcst as cst
    from libcst.codemod import CodemodContext

    module = cst.parse_module(source)
    return command_cls(CodemodContext(), target).transform_module(module).code


def test_serializable_retry_wraps_inside_the_route_decorator():
    """The retry decorator must be innermost. Stacked above @router.post it
    wraps the return value while the router keeps serving the unwrapped
    handler, so the guard would never run on a single request."""
    from raceguard.patch.codemods.base import PatchTarget
    from raceguard.patch.codemods.serializable_retry import SerializableRetryCommand

    source = (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        '@router.post("/orders")\n'
        "async def create_order(item_id: int):\n"
        "    return {}\n"
    )
    target = PatchTarget(handler="create_order", read_line=6, write_line=7)
    out = _run_codemod(SerializableRetryCommand, target, source)
    ast.parse(out)
    route = out.index("@router.post")
    retry = out.index("@retry_on_serialization_failure()")
    defline = out.index("async def create_order")
    assert route < retry < defline, "retry decorator must sit under the route decorator"


def test_serializable_retry_matches_a_sync_handler():
    """A sync `def` handler must get a sync wrapper; the async helper's
    `await fn(...)` would raise TypeError on the first request."""
    from raceguard.patch.codemods.base import PatchTarget
    from raceguard.patch.codemods.serializable_retry import SerializableRetryCommand

    source = (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        '@router.post("/orders")\n'
        "def create_order(item_id: int):\n"
        "    return {}\n"
    )
    target = PatchTarget(handler="create_order", read_line=6, write_line=7, is_async=False)
    out = _run_codemod(SerializableRetryCommand, target, source)
    ast.parse(out)
    assert "time.sleep(" in out
    assert "await fn(" not in out
    assert "import time" in out


def test_emitted_rejection_never_strands_httpexception():
    """A handler that rejects via JSONResponse never imported HTTPException.
    A codemod that emits `raise HTTPException(...)` must import it, or the very
    first rejection is a NameError."""
    from raceguard.patch.codemods.base import PatchTarget
    from raceguard.patch.codemods.optimistic_version import OptimisticVersionCommand

    source = (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        '@router.put("/prefs")\n'
        "async def save_prefs(user_id: int, payload: dict, session):\n"
        "    prefs = await session.get(Prefs, user_id)\n"
        "    prefs.data = payload\n"
        "    await session.commit()\n"
        "    return {}\n"
    )
    read_line = source.splitlines().index("    prefs = await session.get(Prefs, user_id)") + 1
    write_line = source.splitlines().index("    prefs.data = payload") + 1
    target = PatchTarget(
        handler="save_prefs",
        read_line=read_line,
        write_line=write_line,
        model="Prefs",
        column="data",
        pk_value="user_id",
    )
    out = _run_codemod(OptimisticVersionCommand, target, source)
    ast.parse(out)
    assert "raise HTTPException" in out
    assert "import HTTPException" in out  # brought into scope, not left dangling
