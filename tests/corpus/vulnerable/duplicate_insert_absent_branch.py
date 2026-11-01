"""vulnerable / duplicate_insert: The insert sits inside the branch that saw no row."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Follow

router = APIRouter()
get_db = None


@router.post("/follows/branch")
def follow_branch(follower_id: int, followee_id: int, db: Session = Depends(get_db)):
    existing = db.query(Follow).filter(Follow.follower_id == follower_id).first()
    if existing is None:
        db.add(Follow(follower_id=follower_id, followee_id=followee_id))
        db.commit()
