"""safe / plain CRUD: An authorization test on one column followed by a write to another. The write does not depend on the value tested racing with anything."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Item

router = APIRouter()
get_db = None


@router.put("/items/{item_id}")
def edit_item(item_id: int, owner_id: int, sku: str, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item.owner_id != owner_id:
        raise HTTPException(status_code=403)
    item.sku = sku
    db.commit()
