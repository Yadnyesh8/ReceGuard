"""oversell / medium / non_negative

Both the read and the write are pushed into separate helper functions,
with only the threshold decision left inline in the handler. Exercises
def-use linking across two call boundaries at once.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


async def _load_item(session: AsyncSession, item_id: int) -> Item | None:
    return await session.get(Item, item_id)


async def _commit_decrement(session: AsyncSession, item: Item, qty: int) -> None:
    item.stock -= qty
    await session.commit()


@app.post("/orders/two-helpers")
async def create_order_two_helpers(item_id: int, qty: int, session: AsyncSession):
    item = await _load_item(session, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    await _commit_decrement(session, item, qty)
    return {"item_id": item_id, "remaining": item.stock}
