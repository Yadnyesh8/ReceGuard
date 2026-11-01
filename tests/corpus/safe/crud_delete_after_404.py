"""safe / plain CRUD: Deleting a row that was loaded for a 404 check loses nothing; a second delete finds nothing."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Item

router = APIRouter()
get_db = None


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    db.delete(item)
    db.commit()
