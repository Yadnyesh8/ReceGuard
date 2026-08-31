"""duplicate_insert / high / duplicate_insert

A "follow" relationship checked then inserted, with no unique constraint
on (follower_id, followee_id) in models.Follow. A user double-tapping
"follow" (or a retried client request) can create two follow rows for
the same pair, double-counting the relationship anywhere follower counts
are derived from row counts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Follow

router = APIRouter(prefix="/social")


@router.post("/follow")
async def follow_user(follower_id: int, followee_id: int, session: AsyncSession):
    existing = (
        await session.execute(
            select(Follow).where(
                Follow.follower_id == follower_id,
                Follow.followee_id == followee_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"created": False}
    session.add(Follow(follower_id=follower_id, followee_id=followee_id))
    await session.commit()
    return {"created": True}
