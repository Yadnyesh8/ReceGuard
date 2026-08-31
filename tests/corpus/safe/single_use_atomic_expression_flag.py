"""safe / guard: atomic_expression

atomic_expression applied to a boolean flag rather than a numeric
counter: the UPDATE's WHERE clause requires redeemed = False and its SET
clause writes True, all in one atomic statement with no prior SELECT
deciding the outcome. If a concurrent request already redeemed the
coupon, this UPDATE matches zero rows, which the handler checks via
rowcount instead of trusting a stale in-Python read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Coupon

app = FastAPI()


@app.post("/coupons/{coupon_id}/redeem-atomic")
async def redeem_coupon_atomic(coupon_id: int, session: AsyncSession):
    result = await session.execute(
        update(Coupon)
        .where(Coupon.id == coupon_id, Coupon.redeemed.is_(False))
        .values(redeemed=True)
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(409, "coupon already redeemed or does not exist")
    return {"coupon_id": coupon_id, "discount_applied": True}
