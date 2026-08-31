"""duplicate_insert / high / duplicate_insert

Idempotency-key order creation done the naive way: look the key up, and
if it is not found, create the order. `models.Order` has no unique
constraint on idempotency_key, so a retried request racing the original
(the exact scenario idempotency keys exist to prevent) can slip past the
lookup on both sides and create two orders for one client-submitted key.
Compare with safe/duplicate_insert_claim_first.py, which fixes this by
claiming the key first via INSERT ... ON CONFLICT DO NOTHING against a
column that actually has a unique constraint.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order

app = FastAPI()


@app.post("/orders/idempotent")
async def create_order_idempotent(
    user_id: int, idempotency_key: str, total: float, session: AsyncSession
):
    existing = (
        await session.execute(
            select(Order).where(Order.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"order_id": existing.id, "status": existing.status, "created": False}

    order = Order(
        user_id=user_id,
        idempotency_key=idempotency_key,
        total=total,
        status="pending",
    )
    session.add(order)
    await session.commit()
    return {"order_id": order.id, "status": order.status, "created": True}
