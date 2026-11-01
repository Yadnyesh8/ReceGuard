"""safe / plain CRUD: The read value appears only in the WHERE clause. The value stored is the request's, so nothing derived is overwritten."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from models import Item

router = APIRouter()
get_db = None


@router.post("/items/{item_id}/sku")
def relabel_by_id(item_id: int, sku: str, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    db.execute(update(Item).where(Item.id == item.id).values(sku=sku))
    db.commit()
