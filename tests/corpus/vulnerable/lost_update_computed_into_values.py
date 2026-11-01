"""vulnerable / lost_update: The stored value is computed from the read, then written through update().values()."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from models import Account

router = APIRouter()
get_db = None


@router.post("/accounts/{account_id}/credit")
def credit(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    db.execute(
        update(Account).where(Account.id == account_id).values(balance=account.balance + 5)
    )
    db.commit()
