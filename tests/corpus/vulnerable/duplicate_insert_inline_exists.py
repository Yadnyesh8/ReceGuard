"""vulnerable / duplicate_insert: The existence test is on a read that never got a name. Two requests both see nothing and both insert."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Follow

router = APIRouter()
get_db = None


@router.post("/follows/inline")
def follow_inline(follower_id: int, followee_id: int, db: Session = Depends(get_db)):
    if db.query(Follow).filter(Follow.follower_id == follower_id).first():
        raise HTTPException(status_code=409)
    db.add(Follow(follower_id=follower_id, followee_id=followee_id))
    db.commit()
