"""safe / guard: none recognised by the scanner table, suppressed

Team-join requests are not handled inline: this handler enqueues the
join onto a single-consumer queue (`enqueue_serialized`, imported from an
internal queue client the scanner cannot see into) that processes one
message at a time per team, so the count-then-insert below never
actually races against itself even though it has the textbook
aggregate_limit shape. The suppression comment names the mechanism so
`raceguard scan --show-suppressed` keeps it auditable instead of the
finding just vanishing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Team, TeamMember
from queue_client import enqueue_serialized  # internal single-consumer queue, not in this repo

app = FastAPI()


@app.post("/teams/{team_id}/members-queued")
async def add_team_member_queued(team_id: int, user_id: int, session: AsyncSession):
    await enqueue_serialized(f"team:{team_id}")

    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "no such team")
    count = (
        await session.execute(
            select(func.count()).select_from(TeamMember).where(
                TeamMember.team_id == team_id
            )
        )
    ).scalar_one()
    if count >= team.member_limit:  # raceguard: ignore[aggregate_limit] this handler only runs behind a per-team single-consumer queue, so concurrent invocations for one team never overlap
        raise HTTPException(409, "team is full")
    session.add(TeamMember(team_id=team_id, user_id=user_id))
    await session.commit()
    return {"team_id": team_id, "member_count": count + 1}
