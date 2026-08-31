"""safe / guard: unique_constraint

Rather than a boolean flag on the coupon row, redemption is recorded as
an insert into models.UniqueCouponRedemption, whose (coupon_id, user_id)
pair is uniquely constrained. Two concurrent redemption attempts by the
same user race to insert the same redemption row; only one insert
succeeds, and the other's IntegrityError is caught and reported as
"already redeemed" instead of applying the discount twice.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Coupon, UniqueCouponRedemption

app = FastAPI()


@app.post("/coupons/{coupon_id}/redeem-guarded")
async def redeem_coupon_guarded(coupon_id: int, user_id: int, session: AsyncSession):
    coupon = await session.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(404, "no such coupon")

    redemption = UniqueCouponRedemption(coupon_id=coupon_id, user_id=user_id)
    session.add(redemption)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {"discount_applied": False, "reason": "already redeemed"}
    return {"discount_applied": True}
