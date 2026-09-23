"""safe / guard: with_for_update / expected_confidence: medium

Awkward case (implementation plan 17.1): the guard itself -- the
.with_for_update() row lock -- lives inside a helper function
(_load_item_locked) that the handler calls, rather than appearing
directly in the handler's own body. The path is genuinely safe (the
locked read serializes concurrent access to the same row exactly like
with_for_update_oversell.py), but because guard detection has to follow
the call into the helper rather than finding the guard call syntactically
on the handler's own read chain, this is expected to resolve at medium
confidence rather than the high confidence a directly-inlined guard
would get.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


async def _load_item_locked(session: AsyncSession, item_id: int) -> Item | None:
    result = await session.execute(select(Item).where(Item.id == item_id).with_for_update())
    return result.scalar_one_or_none()


@app.post("/orders/guard-in-helper")
async def create_order_guard_in_helper(item_id: int, qty: int, session: AsyncSession):
    item = await _load_item_locked(session, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock -= qty
    await session.commit()
    return {"item_id": item_id, "remaining": item.stock}
