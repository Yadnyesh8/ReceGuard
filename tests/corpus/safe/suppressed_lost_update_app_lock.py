"""safe / guard: none recognised by the scanner table, suppressed

The read-modify-write on the account balance is genuinely protected, but
by a process-local threading.Lock keyed per account id -- a mechanism the
scanner's guard table does not (and, being process-local rather than a
database or distributed primitive, generally should not be trusted to)
recognise. Per pass 4, an unresolved symbol on the path should drop
confidence to low rather than be silently cleared or silently flagged
high; the developer has added an explicit suppression comment naming the
rule and the reason, which `raceguard scan --show-suppressed` will
surface for audit. This fixture is genuinely safe in this single-process
deployment (the lock is real and does serialize access), which is what
the suppression comment claims and what justifies vulnerable: false, but
it is exactly the kind of guard the static scanner cannot verify on its
own, hence the suppression rather than a recognised guard.
"""

import sys
import threading
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account

app = FastAPI()

_account_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


@app.post("/accounts/{account_id}/deposit-app-lock")
async def deposit_app_lock(account_id: int, amount: float, session: AsyncSession):
    with _account_locks[account_id]:
        account = await session.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        current = account.balance
        account.balance = current + amount  # raceguard: ignore[lost_update] guarded by per-account threading.Lock, single-process deployment only
        await session.commit()
        return {"balance": account.balance}
