"""safe / plain CRUD: The stored value comes from the request and does not depend on the row. Concurrent renames are last-write-wins, which is what a client asked for, not a lost update."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Item

router = APIRouter()
get_db = None


@router.patch("/items/{item_id}")
def rename_item(item_id: int, sku: str, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    item.sku = sku
    db.commit()
    return item
