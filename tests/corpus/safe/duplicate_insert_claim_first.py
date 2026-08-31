"""safe / guard: on_conflict

Fixes the exact race in vulnerable/duplicate_insert_idempotency_lookup_
then_act.py by inverting the order of operations: claim the idempotency
key FIRST via INSERT ... ON CONFLICT DO NOTHING against
models.IdempotentOrder, whose idempotency_key column carries a real
unique constraint, then check whether the claim actually inserted a new
row before doing any work. Two concurrent requests with the same key can
both attempt the insert, but Postgres guarantees only one succeeds; the
loser reads back the winner's row instead of creating a second one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import IdempotentOrder

app = FastAPI()


@app.post("/orders/idempotent-claim-first")
async def create_order_claim_first(
    user_id: int, idempotency_key: str, total: float, session: AsyncSession
):
    stmt = (
        pg_insert(IdempotentOrder)
        .values(
            user_id=user_id,
            idempotency_key=idempotency_key,
            total=total,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    await session.execute(stmt)
    await session.commit()

    order = (
        await session.execute(
            select(IdempotentOrder).where(
                IdempotentOrder.idempotency_key == idempotency_key
            )
        )
    ).scalar_one()
    return {"order_id": order.id, "status": order.status}
