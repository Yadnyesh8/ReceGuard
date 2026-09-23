"""safe / guard: version_check

Pairs with vulnerable/lost_update_balance_overwrite.py: a single-account
adjustment that reads the current version alongside the balance, and
whose UPDATE's WHERE clause requires that version to still match. A
concurrent writer that already applied its own update bumps the version,
so this UPDATE matches zero rows instead of overwriting the concurrent
change; the handler surfaces that as a 409 for the client to retry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

app = FastAPI()


@app.post("/accounts/{account_id}/adjust-versioned")
async def adjust_balance_versioned(account_id: int, delta: float, session: AsyncSession):
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(404, "no such account")

    result = await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.version == account.version)
        .values(balance=Account.balance + delta, version=Account.version + 1)
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "concurrent update, please retry")
    return {"account_id": account_id, "applied": True}
