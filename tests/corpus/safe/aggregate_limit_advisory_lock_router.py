"""safe / guard: advisory_lock

Pairs with vulnerable/aggregate_limit_session_cap.py: identical "at most
3 active sessions" check-then-insert, routed through an APIRouter rather
than a bare app, with a per-user Postgres advisory transaction lock
acquired before the count so concurrent session-creation requests for the
same user are serialized.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserSession

router = APIRouter(prefix="/api/v1")

MAX_ACTIVE_SESSIONS = 3


@router.post("/sessions")
async def create_session_locked(user_id: int, session: AsyncSession):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_id}
    )
    active_count = (
        await session.execute(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.active.is_(True),
            )
        )
    ).scalar_one()
    if active_count >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(409, "too many active sessions")
    new_session = UserSession(user_id=user_id, active=True)
    session.add(new_session)
    await session.commit()
    return {"session_id": new_session.id}
