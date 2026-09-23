"""safe / guard: advisory_lock

Pairs with vulnerable/aggregate_limit_team_members.py: identical count-
then-insert shape, but a Postgres advisory transaction lock keyed on the
team id is acquired before the count, so a second concurrent request for
the same team blocks until the first commits and releases the lock. The
count it then sees already includes the first request's insert.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models import Team, TeamMember

app = FastAPI()


@app.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, user_id: int, session: AsyncSession):
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": team_id})

    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "no such team")
    count = (
        await session.execute(
            select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
        )
    ).scalar_one()
    if count >= team.member_limit:
        raise HTTPException(409, "team is full")
    session.add(TeamMember(team_id=team_id, user_id=user_id))
    await session.commit()
    return {"team_id": team_id, "member_count": count + 1}
