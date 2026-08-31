"""safe / guard: raw_for_update

The read is a raw text() query with FOR UPDATE embedded directly in the
SQL literal, which is one of the recognised guard shapes even though the
scanner cannot analyse the query structurally the way it can a
SQLAlchemy select(). The FOR UPDATE keyword sniff on the literal is
enough to mark this path guarded.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()


@app.post("/orders/raw-for-update")
async def create_order_raw_for_update(item_id: int, qty: int, session: AsyncSession):
    result = await session.execute(
        text("SELECT id, stock FROM items WHERE id = :item_id FOR UPDATE"),
        {"item_id": item_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(404, "no such item")
    if row.stock < qty:
        raise HTTPException(409, "not enough stock")
    await session.execute(
        text("UPDATE items SET stock = :new_stock WHERE id = :item_id"),
        {"new_stock": row.stock - qty, "item_id": item_id},
    )
    await session.commit()
    return {"item_id": item_id, "remaining": row.stock - qty}
