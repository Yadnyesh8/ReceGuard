"""VULNERABLE: aggregate_limit (team_members should never exceed teams.max_members).

Count existing members, decide in Python, insert. No constraint anywhere
enforces the cap, so two concurrent joins can both count the same
under-the-limit total and both insert.

This handler is deliberately a plain `def`, not `async def` -- the only one
in the app. FastAPI dispatches `def` handlers to a bounded anyio worker
thread pool instead of running them on the event loop (implementation plan
section 8.2). If a scan configures `concurrency` higher than that pool's
size, requests queue up behind the pool instead of arriving together, and
the race can silently fail to reproduce -- a false negative the harness is
supposed to warn about. We bridge into the async database layer with
`asyncio.run()`, which is safe here specifically because a `def` handler
runs in its own worker thread with no event loop already active.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from shop.db import async_session_maker
from shop.models import Team, TeamMember
from shop.schemas import AddMemberRequest, AddMemberResponse

router = APIRouter()


@router.post("/teams/{team_id}/members", response_model=AddMemberResponse)
def add_team_member(team_id: int, payload: AddMemberRequest) -> AddMemberResponse:
    """A team must never have more members than its max_members limit."""
    return asyncio.run(_add_team_member(team_id, payload))


async def _add_team_member(team_id: int, payload: AddMemberRequest) -> AddMemberResponse:
    async with async_session_maker() as session:
        team = await session.get(Team, team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")

        count = await session.scalar(
            select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id)
        )

        # Read-decide-write window: the count above can go stale before the
        # insert below commits, letting two joins both see room for one more.
        if count >= team.max_members:
            raise HTTPException(status_code=409, detail="team is full")

        member = TeamMember(team_id=team_id, user_email=payload.user_email)
        session.add(member)
        await session.commit()

        return AddMemberResponse(
            team_id=team_id, user_email=payload.user_email, member_count=count + 1
        )
