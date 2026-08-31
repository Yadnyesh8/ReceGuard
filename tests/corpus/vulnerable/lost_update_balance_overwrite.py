"""lost_update / high / conservation

A funds transfer between two accounts, implemented as two independent
read-modify-write pairs with no atomic expression, no SELECT ... FOR
UPDATE, and no version check. Two concurrent transfers touching the same
sender account can each read the same starting balance, and one write
clobbers the other: money leaves the sender once but the receiver can be
credited twice (or the sender's balance ends up wrong), breaking the
"total balance across accounts is conserved" invariant.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

app = FastAPI()


@app.post("/transfers")
async def transfer_funds(
    from_id: int, to_id: int, amount: float, session: AsyncSession
):
    sender = (
        await session.execute(select(Account).where(Account.id == from_id))
    ).scalar_one_or_none()
    receiver = (
        await session.execute(select(Account).where(Account.id == to_id))
    ).scalar_one_or_none()
    if sender is None or receiver is None:
        raise HTTPException(404, "no such account")
    if sender.balance < amount:
        raise HTTPException(409, "insufficient funds")

    new_sender_balance = sender.balance - amount
    new_receiver_balance = receiver.balance + amount
    sender.balance = new_sender_balance
    receiver.balance = new_receiver_balance
    await session.commit()
    return {"sender_balance": new_sender_balance, "receiver_balance": new_receiver_balance}
