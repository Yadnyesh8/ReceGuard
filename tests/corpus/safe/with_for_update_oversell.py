"""safe / guard: with_for_update

The read locks the row with .with_for_update() before the threshold
check, so a second concurrent transaction blocks on the SELECT until the
first commits, and by the time it reads it sees the updated stock. No
oversell is possible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


@app.post("/orders")
async def create_order(item_id: int, qty: int, session: AsyncSession):
    result = await session.execute(
        select(Item).where(Item.id == item_id).with_for_update()
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock -= qty
    await session.commit()
    return {"item_id": item_id, "remaining": item.stock}
