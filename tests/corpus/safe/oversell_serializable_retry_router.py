"""safe / guard: serializable (+ retry_wrapper)

Variety pairing for the serializable+retry guard: the oversell scenario
this time, at SERIALIZABLE isolation with a retry wrapper, routed
through an APIRouter. Two concurrent orders against the same item cause
Postgres to abort one transaction with a serialization failure; the
retry decorator catches it and re-runs the whole read-decide-write, so
the retried request sees the first order's decrement before deciding.
"""

import sys
from pathlib import Path
from functools import wraps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from models import Item

router = APIRouter(prefix="/api/v1")

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
def _do_order(item_id: int, qty: int):
    with Session(engine) as session:
        item = session.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        if item.stock < qty:
            raise HTTPException(409, "not enough stock")
        item.stock -= qty
        session.commit()
        return item.stock


@router.post("/orders-serializable-retried")
def create_order_serializable_retried(item_id: int, qty: int):
    remaining = _do_order(item_id, qty)
    return {"item_id": item_id, "remaining": remaining}
