"""safe / plain CRUD: A threshold on one column gates a write to a different one, so nothing about the compared value is consumed."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Item

router = APIRouter()
get_db = None


@router.post("/items/{item_id}/label")
def relabel_item(item_id: int, sku: str, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item.stock < 10:
        raise HTTPException(status_code=403)
    item.sku = sku
    db.commit()
