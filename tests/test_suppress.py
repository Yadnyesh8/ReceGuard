"""Suppression (section 6.3). The first false positive on a large repo decides
whether the tool stays installed."""

from __future__ import annotations

from raceguard.contracts import Confidence
from raceguard.scanner import ScanOptions, scan
from raceguard.scanner.suppress import ConfigSuppression, inline_match, parse_inline

SOURCE = """
a = 1  # raceguard: ignore[oversell] guarded upstream by the import queue
b = 2  # raceguard: ignore[oversell]
c = 3  # raceguard: ignore[] no rule named
d = 4  # raceguard: ignore[oversell,lost_update] two rules, one reason
e = 5  # raceguard: ignore[*] everything here is serialised
"""


def test_reason_is_required_not_merely_documented():
    found, problems = parse_inline(SOURCE)
    assert any("has no reason" in p for p in problems)
    assert 3 not in found  # the reasonless one is not honoured


def test_rule_id_is_required():
    _found, problems = parse_inline(SOURCE)
    assert any("names no rule" in p for p in problems)


def test_multiple_rules_in_one_comment():
    found, _ = parse_inline(SOURCE)
    entry = found[5]
    assert entry.covers("oversell") and entry.covers("lost_update")
    assert not entry.covers("duplicate_insert")


def test_wildcard():
    found, _ = parse_inline(SOURCE)
    assert found[6].covers("anything_at_all")


def test_suppression_is_rule_scoped():
    """`ignore[oversell]` must not silence a duplicate_insert. A suppression
    that silences everything nearby is how real bugs get hidden."""
    found, _ = parse_inline(SOURCE)
    assert found[2].covers("oversell")
    assert not found[2].covers("duplicate_insert")


def test_inline_match_window():
    found, _ = parse_inline(SOURCE)
    assert inline_match(found, "oversell", [2]) is not None
    assert inline_match(found, "oversell", [3]) is not None  # line above counts
    assert inline_match(found, "oversell", [40]) is None


def test_config_suppression_by_handler():
    rule = ConfigSuppression(
        rule="oversell",
        reason="single admin caller, serialised upstream",
        handler="app/routes/admin.py::bulk_import",
    )
    assert rule.matches(
        rule="oversell", file="app/routes/admin.py", handler="app.routes.admin:bulk_import"
    )
    assert not rule.matches(
        rule="oversell", file="app/routes/orders.py", handler="app.routes.orders:create"
    )
    assert not rule.matches(
        rule="lost_update", file="app/routes/admin.py", handler="app.routes.admin:bulk_import"
    )


def test_config_suppression_by_file():
    rule = ConfigSuppression(
        rule="*", reason="legacy module, scheduled for deletion", file="app/legacy.py"
    )
    assert rule.matches(rule="oversell", file="app/legacy.py", handler="x:y")


def test_suppressed_findings_stay_auditable(tmp_path):
    """--show-suppressed lists everything suppressed with its reason, so a
    suppression cannot quietly become permanent."""
    module = tmp_path / "app.py"
    module.write_text(
        "from fastapi import FastAPI, HTTPException\n"
        "from sqlalchemy import select\n"
        "app = FastAPI()\n"
        '@app.post("/buy")\n'
        "async def buy(item_id: int, qty: int, session):\n"
        "    item = await session.get(Item, item_id)\n"
        "    if item.stock < qty:\n"
        "        raise HTTPException(409, 'no')\n"
        "    item.stock -= qty  # raceguard: ignore[oversell] serialised by the queue\n"
        "    await session.commit()\n"
    )
    result = scan(
        [str(tmp_path)],
        ScanOptions(min_confidence=Confidence.low, show_suppressed=True, root=tmp_path),
    )
    assert not [f for f in result.findings if f.pattern == "oversell"]
    assert any("serialised by the queue" in (f.suppression_reason or "") for f in result.suppressed)
