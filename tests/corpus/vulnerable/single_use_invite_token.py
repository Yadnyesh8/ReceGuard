"""single_use / high / single_use

Same shape as single_use_coupon_redeem, but for an invite token and
routed through an APIRouter rather than a bare app, so both route
decorator forms appear in the vulnerable set. The read-check-flip on
`used` has no guard.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Invite

router = APIRouter(prefix="/invites")


@router.post("/{token}/accept")
async def accept_invite(token: str, session: AsyncSession):
    result = await session.execute(select(Invite).where(Invite.token == token))
    invite = result.scalar_one_or_none()
    if invite is None:
        raise HTTPException(404, "no such invite")
    if invite.used:
        raise HTTPException(409, "invite already used")
    invite.used = True
    await session.commit()
    return {"token": token, "accepted": True}
