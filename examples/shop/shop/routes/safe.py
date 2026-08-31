"""CORRECTLY GUARDED reference endpoint: purchase against shop.models.Product.

Proves RaceGuard does not just flag everything that touches stock. The
decrement and the boundary check happen in one atomic UPDATE statement
(`stock=stock - qty` in the same expression that is filtered by
`stock >= qty`), so PostgreSQL evaluates and applies it as a single
row-locked operation. There is no read-decide-write window: the rowcount
of the UPDATE *is* the decision.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_session
from shop.models import Product
from shop.schemas import PurchaseRequest, PurchaseResponse

router = APIRouter()


@router.post("/products/{product_id}/purchase", response_model=PurchaseResponse)
async def purchase_product(
    product_id: int, payload: PurchaseRequest, session: AsyncSession = Depends(get_session)
) -> PurchaseResponse:
    """A purchase must never reduce a product's stock below zero."""
    result = await session.execute(
        update(Product)
        .where(Product.id == product_id, Product.stock >= payload.qty)
        .values(stock=Product.stock - payload.qty)
    )

    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="insufficient stock or product not found")

    await session.commit()

    product = await session.get(Product, product_id)
    assert product is not None
    return PurchaseResponse(product_id=product_id, qty=payload.qty, remaining_stock=product.stock)
