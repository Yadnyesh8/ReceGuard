"""safe / guard: serializable (+ retry_wrapper)

Pairs with vulnerable/lost_update_serializable_no_retry.py: the same
SERIALIZABLE-isolation deposit, but wrapped in a retry decorator that
catches Postgres's 40001 serialization_failure and re-issues the whole
transaction. Per the guard table, SERIALIZABLE alone is only a partial
guard; the retry_wrapper is what completes it, since it is what turns
"one of the two concurrent deposits gets a 500" into "one of the two
concurrent deposits is transparently retried and both apply."
"""

import sys
from pathlib import Path
from functools import wraps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from models import Account

app = FastAPI()

engine = create_engine(
    "postgresql+psycopg://localhost/raceguard_corpus",
    isolation_level="SERIALIZABLE",
)

SERIALIZATION_FAILURE = "40001"


def retry_on_serialization_failure(max_attempts: int = 3):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as exc:
                    pgcode = getattr(exc.orig, "pgcode", None)
                    if pgcode == SERIALIZATION_FAILURE and attempt < max_attempts - 1:
                        continue
                    raise

        return wrapper

    return decorator


@retry_on_serialization_failure()
def _do_deposit(account_id: int, amount: float):
    with Session(engine) as session:
        account = session.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "no such account")
        current = account.balance
        account.balance = current + amount
        session.commit()
        return account.balance


@app.post("/accounts/{account_id}/deposit-serializable-retried")
def deposit_serializable_retried(account_id: int, amount: float):
    balance = _do_deposit(account_id, amount)
    return {"balance": balance}
