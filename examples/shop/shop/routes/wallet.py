"""VULNERABLE: lost update (conservation on accounts.balance).

Read both balances, decide in Python, write both. No row lock, no atomic
UPDATE expression. Two concurrent transfers can both read the same
pre-debit balance, so the sum across the pair drifts from what it should be.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_session
from shop.models import Account
from shop.schemas import TransferRequest, TransferResponse

router = APIRouter()


@router.post("/wallet/transfer", response_model=TransferResponse)
async def transfer(
    payload: TransferRequest, session: AsyncSession = Depends(get_session)
) -> TransferResponse:
    """A transfer must never change the total balance held across the two accounts."""
    from_acct = await session.get(Account, payload.from_id)
    to_acct = await session.get(Account, payload.to_id)
    if from_acct is None or to_acct is None:
        raise HTTPException(status_code=404, detail="account not found")

    if from_acct.balance < payload.amount:
        raise HTTPException(status_code=409, detail="insufficient balance")

    # Read-decide-write window on two rows at once: the debit and the credit
    # are both computed from values read before this transaction commits.
    from_acct.balance -= payload.amount
    to_acct.balance += payload.amount
    await session.commit()

    return TransferResponse(
        from_id=from_acct.id,
        to_id=to_acct.id,
        amount=payload.amount,
        from_balance=from_acct.balance,
        to_balance=to_acct.balance,
    )
