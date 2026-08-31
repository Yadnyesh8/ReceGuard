"""oversell / medium / non_negative

The read and the threshold decision happen inline in the handler, but the
write is issued from a helper function one call away. Pass 3 of the
scanner records writes that cross a call boundary at medium confidence
rather than high, since it does not attempt interprocedural analysis in
v1.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


async def _apply_decrement(session: AsyncSession, item: Item, qty: int) -> None:
    item.stock -= qty
    await session.commit()


@app.post("/orders/helper-write")
async def create_order_helper_write(item_id: int, qty: int, session: AsyncSession):
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    await _apply_decrement(session, item, qty)
    return {"item_id": item_id, "remaining": item.stock}
