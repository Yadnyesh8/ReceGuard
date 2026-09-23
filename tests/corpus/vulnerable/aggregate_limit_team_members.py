"""aggregate_limit / high / aggregate_limit

"At most member_limit members per team" checked by counting existing
team_members rows and comparing against the limit before inserting a
new one, with no lock and no constraint backing the count. Two
concurrent join requests for a nearly-full team can both count the same
pre-join total, both pass the check, and both insert, overshooting the
limit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Team, TeamMember

app = FastAPI()


@app.post("/teams/{team_id}/members")
async def add_team_member(team_id: int, user_id: int, session: AsyncSession):
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
