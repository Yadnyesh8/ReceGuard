"""safe / guard: version_check

Awkward case (implementation plan 17.1): a plain `def` handler that
bridges into an async engine via asyncio.run() around a small async
helper, rather than declaring `async def` and taking an AsyncSession
directly. Structurally this is still one linked read-decide-write chain
(the bridge function is the only thing between the handler and the
session), and the update's WHERE clause pins the row to the exact
`version` value that was read, so a concurrent writer that already
bumped the version causes this UPDATE to match zero rows instead of
silently clobbering the other write. The handler treats rowcount == 0 as
a conflict to retry client-side, which is optimistic concurrency done
correctly.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

app = FastAPI()


async def _versioned_deposit(session: AsyncSession, account_id: int, amount: float):
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        return None
    read_version = account.version
    result = await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.version == read_version)
        .values(balance=Account.balance + amount, version=Account.version + 1)
    )
    await session.commit()
    return result.rowcount


def _run_versioned_deposit(session: AsyncSession, account_id: int, amount: float):
    return asyncio.run(_versioned_deposit(session, account_id, amount))


@app.post("/accounts/{account_id}/deposit-versioned-bridge")
def deposit_versioned_bridge(account_id: int, amount: float, session: AsyncSession):
    rowcount = _run_versioned_deposit(session, account_id, amount)
    if rowcount is None:
        raise HTTPException(404, "no such account")
    if rowcount == 0:
        raise HTTPException(409, "concurrent update, please retry")
    return {"account_id": account_id, "applied": True}
