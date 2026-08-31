"""lost_update / medium / conservation

The read is pushed into a helper function (_load_account) one call away
from the handler; the blind overwrite write stays inline. This is the
mirror image of oversell_helper_write.py (which pushes the WRITE into a
helper): here it is the READ side of the def-use chain that crosses a
call boundary, which pass 3 also caps at medium confidence since v1 does
not do interprocedural analysis.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

app = FastAPI()


async def _load_account(session: AsyncSession, account_id: int) -> Account | None:
    return await session.get(Account, account_id)


@app.post("/accounts/{account_id}/withdraw-helper-read")
async def withdraw_helper_read(account_id: int, amount: float, session: AsyncSession):
    account = await _load_account(session, account_id)
    if account is None:
        raise HTTPException(404, "no such account")
    new_balance = account.balance - amount
    account.balance = new_balance
    await session.commit()
    return {"balance": new_balance}
