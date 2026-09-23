"""Hygiene rules (section 10), especially the one that belongs in a race
condition tool: an idempotency check that is itself a check-then-act race."""

from __future__ import annotations

from pathlib import Path

import pytest

from raceguard.hygiene import check
from raceguard.hygiene.limiter import LimiterSpec, generate

UNSAFE_IDEMPOTENCY = """
from fastapi import APIRouter, Header
from sqlalchemy import select
router = APIRouter()

@router.post("/pay")
async def pay(idempotency_key: str = Header(...), session=None):
    existing = await session.scalar(select(IdemKey).where(IdemKey.key == idempotency_key))
    if existing:
        return existing.response
    result = await do_work()
    session.add(IdemKey(key=idempotency_key, response=result))
    return result
"""

SAFE_IDEMPOTENCY = """
from fastapi import APIRouter, Header
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
router = APIRouter()

@router.post("/pay")
async def pay(idempotency_key: str = Header(...), session=None):
    try:
        await session.execute(insert(IdemKey).values(key=idempotency_key, state="in_progress"))
        await session.commit()
    except IntegrityError:
        return await replay(idempotency_key)
    return await do_work()
"""

SWALLOWED = """
from fastapi import APIRouter
router = APIRouter()

@router.post("/x")
async def x(session=None):
    try:
        await session.commit()
    except SerializationFailure:
        pass
"""

LIMITED = """
from fastapi import FastAPI
from slowapi import Limiter
app = FastAPI()
limiter = Limiter(key_func=lambda r: r.client.host)

@app.post("/x")
async def x(session=None):
    session.add(Thing())
"""


def write(tmp_path: Path, name: str, source: str) -> Path:
    (tmp_path / name).write_text(source)
    return tmp_path


def rules_for(tmp_path: Path) -> set[str]:
    return {f.rule for f in check([str(tmp_path)], root=tmp_path)}


def test_lookup_then_act_is_flagged_high(tmp_path):
    write(tmp_path, "unsafe.py", UNSAFE_IDEMPOTENCY)
    findings = check([str(tmp_path)], root=tmp_path)
    unsafe = [f for f in findings if f.rule == "unsafe_idempotency_check"]
    assert unsafe and unsafe[0].severity == "high"
    assert "both miss the lookup" in unsafe[0].explanation


def test_claim_first_is_not_flagged(tmp_path):
    write(tmp_path, "safe.py", SAFE_IDEMPOTENCY)
    assert "unsafe_idempotency_check" not in rules_for(tmp_path)


def test_missing_idempotency_on_a_creating_route(tmp_path):
    (tmp_path / "create.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        '@router.post("/create")\nasync def create(session=None):\n'
        "    session.add(Order())\n"
    )
    assert "missing_idempotency_key" in rules_for(tmp_path)


def test_swallowed_serialization_failure(tmp_path):
    write(tmp_path, "swallow.py", SWALLOWED)
    findings = check([str(tmp_path)], root=tmp_path)
    swallowed = [f for f in findings if f.rule == "swallowed_serialization_failure"]
    assert swallowed and swallowed[0].severity == "high"
    assert "discards that transaction's writes" in swallowed[0].explanation


def test_missing_rate_limit_is_reported_once_per_app(tmp_path):
    """Forty identical findings is noise, not signal."""
    for index in range(4):
        (tmp_path / f"r{index}.py").write_text(
            "from fastapi import APIRouter\nrouter = APIRouter()\n"
            f'@router.post("/r{index}")\nasync def r{index}(session=None):\n'
            "    session.add(Thing())\n"
        )
    findings = check([str(tmp_path)], root=tmp_path)
    limiter = [f for f in findings if f.rule == "missing_rate_limit"]
    assert len(limiter) == 1
    assert limiter[0].route == "*"


def test_a_detected_limiter_silences_the_rule(tmp_path):
    write(tmp_path, "limited.py", LIMITED)
    assert "missing_rate_limit" not in rules_for(tmp_path)


def test_reverse_proxy_config_counts_as_a_limiter(tmp_path):
    (tmp_path / "app.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        '@router.post("/x")\nasync def x(session=None):\n    session.add(T())\n'
    )
    (tmp_path / "nginx.conf").write_text(
        "limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;\n"
    )
    assert "missing_rate_limit" not in rules_for(tmp_path)


def test_missing_optimistic_concurrency_on_a_full_body_put(tmp_path):
    (tmp_path / "docs.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        '@router.put("/docs/{doc_id}")\n'
        "async def update(doc_id: int, body: DocBody, session=None):\n"
        "    doc = await session.get(Doc, doc_id)\n"
        "    doc.title = body.title\n"
        "    await session.commit()\n"
    )
    assert "missing_optimistic_concurrency" in rules_for(tmp_path)


def test_if_match_header_satisfies_the_rule(tmp_path):
    (tmp_path / "docs.py").write_text(
        "from fastapi import APIRouter, Header\nrouter = APIRouter()\n"
        '@router.put("/docs/{doc_id}")\n'
        "async def update(doc_id: int, body: DocBody, if_match: str = Header(None), session=None):\n"
        "    doc = await session.get(Doc, doc_id)\n"
        "    await session.commit()\n"
    )
    assert "missing_optimistic_concurrency" not in rules_for(tmp_path)


# -- the generated limiter ------------------------------------------------


@pytest.mark.parametrize("distributed", [False, True])
def test_generated_limiter_compiles(distributed):
    name, source = generate(LimiterSpec(distributed=distributed))
    compile(source, name, "exec")


@pytest.mark.parametrize("distributed", [False, True])
def test_generated_limiter_advertises_both_header_spellings(distributed):
    _name, source = generate(LimiterSpec(distributed=distributed))
    assert "RateLimit-Limit" in source  # de facto deployed convention
    assert "RateLimit-Policy" in source  # IETF httpapi draft
    assert "Retry-After" in source


def test_in_process_limiter_states_its_own_limitation():
    """N workers give N times the limit. Generated code that hides that is
    worse than no generated code."""
    _name, source = generate(LimiterSpec(distributed=False))
    assert "N times the configured" in source


def test_distributed_limiter_is_atomic():
    """A read-then-write limiter has the very race this tool exists to find."""
    _name, source = generate(LimiterSpec(distributed=True))
    assert "TOKEN_BUCKET_LUA" in source
    assert "atomically" in source.lower()
