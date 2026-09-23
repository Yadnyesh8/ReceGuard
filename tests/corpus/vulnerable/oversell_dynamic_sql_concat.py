"""oversell / low / non_negative

The write is a real, unguarded read-decide-write oversell, but the SQL
text is assembled by runtime string concatenation rather than the
SQLAlchemy expression language. The scanner cannot statically resolve
what the query does, so this must land at LOW confidence: it should
neither be cleared (the bug is real) nor flagged at high or medium (the
scanner has no static basis to be that confident).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models import Item

app = FastAPI()


@app.post("/orders/dynamic")
async def create_order_dynamic(item_id: int, qty: int, session: AsyncSession):
    read_sql = "SELECT stock FROM items WHERE id = " + str(item_id)
    result = await session.execute(text(read_sql))
    row = result.first()
    if row is None:
        raise HTTPException(404, "no such item")
    stock = row[0]
    if stock < qty:
        raise HTTPException(409, "not enough stock")
    table = "items"
    column = "stock"
    write_sql = (
        "UPDATE "
        + table
        + " SET "
        + column
        + " = "
        + str(stock - qty)
        + " WHERE id = "
        + str(item_id)
    )
    await session.execute(text(write_sql))
    await session.commit()
    return {"item_id": item_id, "remaining": stock - qty}
