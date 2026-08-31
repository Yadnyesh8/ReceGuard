"""aggregate_limit / medium / aggregate_limit

"At most 3 active sessions per user" checked inline, but the insert that
actually creates the new session is issued from a helper function one
call away, which pass 3 caps at medium confidence. Pairs with
safe/aggregate_limit_advisory_lock_router.py, which guards the identical
scenario with pg_advisory_xact_lock.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserSession

app = FastAPI()

MAX_ACTIVE_SESSIONS = 3


async def _insert_session(session: AsyncSession, user_id: int) -> UserSession:
    new_session = UserSession(user_id=user_id, active=True)
    session.add(new_session)
    await session.commit()
    return new_session


@app.post("/sessions")
async def create_session(user_id: int, session: AsyncSession):
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
    new_session = await _insert_session(session, user_id)
    return {"session_id": new_session.id}
