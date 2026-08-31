"""lost_update / high / conservation

Awkward case (implementation plan 17.1): an async def handler that uses a
SYNCHRONOUS SQLAlchemy Session rather than AsyncSession. FastAPI will run
the blocking session calls on the event loop thread instead of the
threadpool it would use for a plain `def` handler, which changes the
interleaving behaviour under load but does not change the underlying bug:
this is still a blind read-modify-write deposit with no atomic increment,
no SELECT ... FOR UPDATE, and no version check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.orm import Session

from models import Account

app = FastAPI()


@app.post("/accounts/{account_id}/deposit")
async def deposit(account_id: int, amount: float, session: Session):
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "no such account")
    current = account.balance
    account.balance = current + amount
    session.commit()
    return {"balance": account.balance}
