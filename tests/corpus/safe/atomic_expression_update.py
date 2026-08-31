"""safe / guard: atomic_expression

Deliberately shaped to look flaggable to a naive matcher: there IS a
SELECT that reads stock, followed by an UPDATE, which is exactly the
read-then-write shape the scanner looks for. What makes it safe is that
the UPDATE's WHERE clause re-checks the threshold and its SET clause is a
self-referencing column expression (stock=Item.stock - qty) evaluated
atomically by Postgres against the current row, not a Python-computed
literal. If a concurrent transaction already dropped stock below qty,
this UPDATE simply matches zero rows and rowcount is checked to detect
that. A matcher that only looks for "SELECT column, then UPDATE with a
new value" without inspecting whether the SET expression references the
column itself will flag this -- and it must not, once atomic_expression
detection is applied.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


@app.post("/orders/atomic")
async def create_order_atomic(item_id: int, qty: int, session: AsyncSession):
    item = (
        await session.execute(select(Item).where(Item.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "no such item")

    result = await session.execute(
        update(Item)
        .where(Item.id == item_id, Item.stock >= qty)
        .values(stock=Item.stock - qty)
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "not enough stock")
    return {"item_id": item_id, "decremented_by": qty}
