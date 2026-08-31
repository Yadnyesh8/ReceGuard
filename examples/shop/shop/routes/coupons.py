"""VULNERABLE: single_use (coupon_redemptions should hold at most one row per coupon).

Count existing redemptions, decide in Python, insert. No unique constraint
on coupon_redemptions.coupon_id and no row lock on the coupon, so two
concurrent redemptions can both count zero prior redemptions and both insert.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_session
from shop.models import Coupon, CouponRedemption
from shop.schemas import RedeemCouponRequest, RedeemCouponResponse

router = APIRouter()


@router.post("/coupons/redeem", response_model=RedeemCouponResponse)
async def redeem_coupon(
    payload: RedeemCouponRequest, session: AsyncSession = Depends(get_session)
) -> RedeemCouponResponse:
    """A coupon must never be redeemed more times than its max_redemptions."""
    coupon = await session.scalar(select(Coupon).where(Coupon.code == payload.code))
    if coupon is None:
        raise HTTPException(status_code=404, detail="coupon not found")

    used = await session.scalar(
        select(func.count())
        .select_from(CouponRedemption)
        .where(CouponRedemption.coupon_id == coupon.id)
    )

    # Read-decide-write window: the count above can go stale before the
    # insert below commits, letting two requests both see room for one more.
    if used >= coupon.max_redemptions:
        raise HTTPException(status_code=409, detail="coupon already redeemed")

    redemption = CouponRedemption(coupon_id=coupon.id, redeemed_by=payload.redeemed_by)
    session.add(redemption)
    await session.commit()

    return RedeemCouponResponse(
        code=coupon.code, redeemed_by=payload.redeemed_by, redemption_id=redemption.id
    )
