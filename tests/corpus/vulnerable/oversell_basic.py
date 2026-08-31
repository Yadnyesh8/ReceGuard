"""oversell / high / non_negative

Textbook read-check-write on a single stock counter, in one scope, no
guard anywhere on the path. Two concurrent requests can both observe
enough stock and both decrement, overselling by one unit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


@app.post("/orders")
async def create_order(item_id: int, qty: int, session: AsyncSession):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock -= qty
    await session.commit()
    return {"item_id": item_id, "remaining": item.stock}
