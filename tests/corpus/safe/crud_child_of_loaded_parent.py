"""safe / plain CRUD: Loading a parent row and creating a child is the most common handler there is. Past the 404 the row is known to exist, so this is not create-if-absent."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Item, Order

router = APIRouter()
get_db = None


@router.post("/items/{item_id}/orders")
def create_note(item_id: int, qty: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    order = Order(item_id=item.id, qty=qty)
    db.add(order)
    db.commit()
    return order
