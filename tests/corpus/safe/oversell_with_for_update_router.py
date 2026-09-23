"""safe / guard: with_for_update

Same guard as with_for_update_oversell.py, expressed through an
APIRouter with a path prefix instead of a bare app, so with_for_update
coverage exists on both route decorator forms just like the oversell
vulnerable set does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

router = APIRouter(prefix="/api/v1")


@router.post("/orders-locked")
async def create_order_locked(item_id: int, qty: int, session: AsyncSession):
    result = await session.execute(select(Item).where(Item.id == item_id).with_for_update())
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock -= qty
    await session.commit()
    return {"item_id": item_id, "remaining": item.stock}
