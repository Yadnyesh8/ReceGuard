"""lost_update / medium / conservation

guard_partial: serializable

The engine is opened at SERIALIZABLE isolation, which per the guard table
is only a PARTIAL guard on its own: under real contention Postgres aborts
one of the two conflicting transactions with a 40001 serialization
failure, and this handler does not catch it or retry. The write is still
a bare read-modify-write with no atomic expression. Without a retry
wrapper, a concurrent deposit does not silently corrupt the balance, but
it does surface as an unhandled 5xx to the caller and the deposit is
simply lost -- which is exactly the "swallowed_serialization_failure /
unbounded_retry" gap the guard table calls out, so this must not be
scored as a full guard.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Account

app = FastAPI()

engine = create_engine(
    "postgresql+psycopg://localhost/raceguard_corpus",
    isolation_level="SERIALIZABLE",
)


@app.post("/accounts/{account_id}/deposit-serializable")
def deposit_serializable(account_id: int, amount: float):
    with Session(engine) as session:
        account = session.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        current = account.balance
        account.balance = current + amount
        session.commit()
        return {"balance": account.balance}
