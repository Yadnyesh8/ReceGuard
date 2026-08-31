"""single_use / high / single_use

A coupon carries a `redeemed` boolean. The handler reads it, decides
based on it, and flips it in a separate write with no atomic
compare-and-set and no row lock. Two concurrent redemptions can both
observe redeemed=False and both apply the discount, consuming a
single-use coupon twice.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Coupon

app = FastAPI()


@app.post("/coupons/{coupon_id}/redeem")
async def redeem_coupon(coupon_id: int, session: AsyncSession):
    coupon = await session.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(404, "no such coupon")
    if coupon.redeemed:
        raise HTTPException(409, "coupon already redeemed")
    coupon.redeemed = True
    await session.commit()
    return {"coupon_id": coupon_id, "discount_applied": True}
