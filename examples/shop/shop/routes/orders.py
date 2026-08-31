"""VULNERABLE: oversell (non_negative on items.stock).

Read stock, decide in Python, write. No row lock, no atomic expression,
no CHECK constraint on items.stock. Compare with shop/routes/safe.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_session
from shop.models import Item, Order
from shop.schemas import CreateOrderRequest, OrderResponse

router = APIRouter()


@router.post("/orders", response_model=OrderResponse)
async def create_order(
    payload: CreateOrderRequest, session: AsyncSession = Depends(get_session)
) -> OrderResponse:
    """An order must never reduce an item's stock below zero."""
    item = await session.get(Item, payload.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    # Read-decide-write window: two concurrent requests can both pass this
    # check against the same pre-decrement stock value before either commits.
    if item.stock < payload.qty:
        raise HTTPException(status_code=409, detail="insufficient stock")

    item.stock -= payload.qty
    session.add(Order(item_id=item.id, qty=payload.qty))
    await session.commit()

    return OrderResponse(item_id=item.id, qty=payload.qty, remaining_stock=item.stock)
