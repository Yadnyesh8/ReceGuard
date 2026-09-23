"""oversell / high / non_negative

Read via session.execute(select(...)) instead of session.get, and write
via session.execute(update(...)) with a plain literal value rather than
an atomic column expression, so the guard table's atomic_expression
detector correctly does NOT fire here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


@app.post("/orders/execute-form")
async def create_order_execute(item_id: int, qty: int, session: AsyncSession):
    result = await session.execute(select(Item).where(Item.id == item_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    new_stock = item.stock - qty
    await session.execute(update(Item).where(Item.id == item_id).values(stock=new_stock))
    await session.commit()
    return {"item_id": item_id, "remaining": new_stock}
