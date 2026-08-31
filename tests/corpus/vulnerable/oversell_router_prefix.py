"""oversell / high / non_negative

Same bug as oversell_basic, expressed through an APIRouter with a path
prefix rather than a bare FastAPI app, to exercise the composed-path
resolution in pass 1 (router.post + APIRouter(prefix=...)).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

router = APIRouter(prefix="/api/v1")


@router.post("/orders")
async def create_order_v1(item_id: int, qty: int, session: AsyncSession):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock -= qty
    await session.commit()
    return {"item_id": item_id, "remaining": item.stock}
