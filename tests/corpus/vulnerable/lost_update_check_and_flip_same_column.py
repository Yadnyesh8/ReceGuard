"""vulnerable / lost_update: The written value is a constant, but a test of that same column chose it, so two requests can both act on the old value."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Account

router = APIRouter()
get_db = None


@router.post("/accounts/{account_id}/tier")
def publish(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account.tier == "free":
        account.tier = "pro"
    db.commit()
