"""safe / plain CRUD: payload.get() is a dict lookup, not a database read."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Signup

router = APIRouter()
get_db = None


@router.post("/signups/raw")
async def create_from_payload(payload: dict, db: Session = Depends(get_db)):
    email = payload.get("email")
    db.add(Signup(email=email))
    db.commit()
