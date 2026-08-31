"""VULNERABLE: duplicate_insert (users should hold at most one row per email).

Look up by email, decide in Python, insert. No unique index on users.email
(see migrations/versions/0001_initial.py), so two concurrent signups with
the same email can both find nothing and both insert.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shop.db import get_session
from shop.models import User
from shop.schemas import SignupRequest, SignupResponse

router = APIRouter()


@router.post("/signup", response_model=SignupResponse)
async def signup(
    payload: SignupRequest, session: AsyncSession = Depends(get_session)
) -> SignupResponse:
    """A user account must be created at most once per email address."""
    existing = await session.scalar(select(User).where(User.email == payload.email))

    # Read-decide-write window: the lookup above can both come back empty
    # for two concurrent requests before either insert commits.
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    user = User(email=payload.email)
    session.add(user)
    await session.commit()

    return SignupResponse(id=user.id, email=user.email)
