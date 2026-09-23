"""Scanner behaviour, including the section 17.2 quality gates."""

from __future__ import annotations

import libcst as cst
import pytest
from eval_corpus import FP_GATE, RECALL_GATE
from eval_corpus import run as run_eval

from raceguard.contracts import Confidence
from raceguard.scanner import ScanOptions, scan
from raceguard.scanner.matchers import Role, classify_call
from raceguard.scanner.routes import collect_routes, compose_route

# -- the gates ------------------------------------------------------------


@pytest.fixture(scope="module")
def evaluation():
    return run_eval()


def test_recall_gate(evaluation):
    assert evaluation.recall >= RECALL_GATE, (
        f"recall {evaluation.recall:.1%} is below the {RECALL_GATE:.0%} gate. "
        f"Missed: {evaluation.missed}"
    )


def test_false_positive_gate(evaluation):
    assert evaluation.fp_rate_high <= FP_GATE, (
        f"false positive rate at high confidence is {evaluation.fp_rate_high:.1%}, "
        f"above the {FP_GATE:.0%} gate. {evaluation.false_positives_high}"
    )


def test_scan_reports_no_errors(evaluation):
    assert not evaluation.errors, evaluation.errors


# -- route resolution -----------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            'from fastapi import FastAPI\napp = FastAPI()\n@app.post("/orders")\nasync def h(): pass\n',
            "POST /orders",
        ),
        (
            'from fastapi import APIRouter\nr = APIRouter(prefix="/v1")\n@r.patch("/items/{id}")\nasync def h(): pass\n',
            "PATCH /v1/items/{id}",
        ),
        (
            'r = APIRouter()\n@r.api_route("/x", methods=["DELETE"])\nasync def h(): pass\n',
            "DELETE /x",
        ),
    ],
)
def test_route_composition(source, expected):
    collector = collect_routes(cst.parse_module(source), "m")
    handler = next(h for h in collector.handlers if h.routes)
    assert compose_route(handler, collector) == expected


def test_unresolvable_prefix_is_none_not_a_guess():
    """An unresolved prefix is recorded as None. Guessing produces a route that
    looks authoritative and is wrong, which is worse than admitting ignorance."""
    source = (
        "from fastapi import APIRouter\n"
        "r = APIRouter(prefix=PREFIX)\n"
        '@r.post("/x")\n'
        "async def h(): pass\n"
    )
    collector = collect_routes(cst.parse_module(source), "m")
    handler = next(h for h in collector.handlers if h.routes)
    assert compose_route(handler, collector) is None


def test_async_and_sync_handlers_are_distinguished():
    source = (
        "from fastapi import FastAPI\napp = FastAPI()\n"
        '@app.post("/a")\nasync def a(): pass\n'
        '@app.post("/b")\ndef b(): pass\n'
    )
    collector = collect_routes(cst.parse_module(source), "m")
    kinds = {h.name: h.is_async for h in collector.handlers}
    assert kinds == {"a": True, "b": False}


# -- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "source,role",
    [
        ("session.get(Item, item_id)", Role.read),
        ("session.execute(select(Item).where(Item.id == 1))", Role.read),
        ("session.execute(update(Item).values(stock=1))", Role.write),
        ("session.execute(insert(User).values(email=e))", Role.write),
        ("session.add(Order(id=1))", Role.write),
        ('session.execute(text("UPDATE items SET stock = 0"))', Role.write),
        ('session.execute(text("SELECT 1 FROM items FOR UPDATE"))', Role.read),
        ("db.query(Item).filter(Item.id == 1).first()", Role.read),
    ],
)
def test_call_classification(source, role):
    assert classify_call(cst.parse_expression(source), 1, 1).role is role


def test_for_update_inside_a_select_is_not_a_write():
    """`FOR UPDATE` contains the word UPDATE. A naive keyword sniff calls the
    statement a write, loses the read, and the finding disappears."""
    site = classify_call(
        cst.parse_expression('session.execute(text("SELECT stock FROM items FOR UPDATE"))'), 1, 1
    )
    assert site.role is Role.read
    assert "raw_for_update" in site.guards


def test_dynamic_sql_is_flagged_unresolvable():
    site = classify_call(cst.parse_expression("session.execute(text(build_query()))"), 1, 1)
    assert site.dynamic_sql
    assert site.role is Role.unknown


# -- end to end on the reference app --------------------------------------


def test_shop_findings(shop_scan):
    routes = {(f.route, f.pattern) for f in shop_scan.findings}
    assert ("POST /orders", "oversell") in routes
    assert ("POST /signup", "duplicate_insert") in routes
    assert ("POST /teams/{team_id}/members", "aggregate_limit") in routes


def test_guarded_endpoint_is_not_flagged(shop_scan):
    """safe.py does the check and the decrement in one atomic UPDATE. Flagging
    it would mean the tool cannot tell a fix from a bug."""
    flagged = {f.route for f in shop_scan.findings}
    assert not any(route and "purchase" in route for route in flagged), flagged


def test_helper_delegation_is_medium_not_high(shop_scan):
    """The teams handler delegates to a helper. The window is real, but the
    link crosses a call boundary, so high confidence would be an overclaim."""
    finding = next(f for f in shop_scan.findings if f.route == "POST /teams/{team_id}/members")
    assert finding.confidence is Confidence.medium
    assert "call boundary" in finding.explanation


def test_findings_are_stable_across_runs(shop, shop_scan):
    again = scan([str(shop / "shop")], ScanOptions(min_confidence=Confidence.low, root=shop))
    assert [f.id for f in again.findings] == [f.id for f in shop_scan.findings]


def test_min_confidence_filters(shop):
    high = scan([str(shop / "shop")], ScanOptions(min_confidence=Confidence.high, root=shop))
    low = scan([str(shop / "shop")], ScanOptions(min_confidence=Confidence.low, root=shop))
    assert len(high.findings) < len(low.findings)


def test_two_handlers_sharing_a_name_are_both_reported(tmp_path):
    """A name reused for two handlers in one module (or across router files)
    must not collapse into one finding. Keying the dedupe on the handler name
    alone dropped the race on the second, the worst failure a race detector
    has: a real defect reported as absent."""
    source = (
        "from fastapi import APIRouter, HTTPException\n"
        "router = APIRouter()\n\n"
        '@router.post("/a")\n'
        "async def handler(item_id: int, qty: int, session):\n"
        "    item = await session.get(Item, item_id)\n"
        "    if item.stock < qty:\n"
        '        raise HTTPException(409, "no")\n'
        "    item.stock -= qty\n"
        "    await session.commit()\n"
        "    return {}\n\n"
        '@router.post("/b")\n'
        "async def handler(item_id: int, qty: int, session):\n"
        "    acct = await session.get(Account, item_id)\n"
        "    if acct.balance < qty:\n"
        '        raise HTTPException(409, "no")\n'
        "    acct.balance -= qty\n"
        "    await session.commit()\n"
        "    return {}\n"
    )
    (tmp_path / "app.py").write_text(source)
    result = scan([str(tmp_path)], ScanOptions(min_confidence=Confidence.low, root=tmp_path))
    routes = sorted(f.route for f in result.findings if f.pattern == "oversell")
    assert routes == ["POST /a", "POST /b"], f"both handlers must be reported, got {routes}"
