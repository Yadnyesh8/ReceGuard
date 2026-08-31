"""oversell / high / non_negative

Same bug shape as oversell_basic, but the read uses the legacy
Query.filter().first() call shape instead of session.get(), to exercise
that matcher independently.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.orm import Session

from models import Item

app = FastAPI()


@app.post("/orders/legacy")
def create_order_legacy(item_id: int, qty: int, session: Session):
    item = session.query(Item).filter(Item.id == item_id).first()
    if item is None:
        raise HTTPException(404, "no such item")
    if item.stock < qty:
        raise HTTPException(409, "not enough stock")
    item.stock = item.stock - qty
    session.commit()
    return {"item_id": item_id, "remaining": item.stock}
