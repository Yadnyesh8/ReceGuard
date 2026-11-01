"""safe / plain CRUD: A state word in the test, but nothing is flipped: the write touches another column."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Account

router = APIRouter()
get_db = None


@router.post("/accounts/{account_id}/name")
def rename_account(account_id: int, name: str, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account.active:
        raise HTTPException(status_code=403)
    account.name = name
    db.commit()
