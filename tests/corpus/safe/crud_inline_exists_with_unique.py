"""safe / plain CRUD: An inline existence test, with the unique constraint doing the real work."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import UniqueSignup

router = APIRouter()
get_db = None


@router.post("/unique-signups")
def signup_unique(email: str, db: Session = Depends(get_db)):
    if db.query(UniqueSignup).filter(UniqueSignup.email == email).first():
        raise HTTPException(status_code=409)
    db.add(UniqueSignup(email=email))
    db.commit()
