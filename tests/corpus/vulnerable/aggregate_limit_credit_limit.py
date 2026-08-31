"""aggregate_limit / high / aggregate_limit

A credit-limit check: sum the user's existing pending charges, and only
allow a new charge if the sum plus the new amount stays under
credit_limit. The sum and the insert are two separate statements with no
lock and no constraint tying them together, so two concurrent charge
requests can each sum the same pre-charge total, both pass the check,
and both insert, pushing the user's total pending charges over their
credit limit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import CreditAccount, PendingCharge

app = FastAPI()


@app.post("/charges")
async def create_charge(user_id: int, amount: float, session: AsyncSession):
    credit = (
        await session.execute(
            select(CreditAccount).where(CreditAccount.user_id == user_id)
        )
    ).scalar_one_or_none()
    if credit is None:
        raise HTTPException(404, "no such credit account")
    outstanding = (
        await session.execute(
            select(func.coalesce(func.sum(PendingCharge.amount), 0)).where(
                PendingCharge.user_id == user_id
            )
        )
    ).scalar_one()
    if outstanding + amount > credit.credit_limit:
        raise HTTPException(409, "over credit limit")
    session.add(PendingCharge(user_id=user_id, amount=amount))
    await session.commit()
    return {"user_id": user_id, "outstanding": float(outstanding) + amount}
