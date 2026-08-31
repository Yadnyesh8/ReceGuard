"""safe / guard: advisory_lock

Pairs conceptually with vulnerable/duplicate_insert_signup.py: same
check-then-insert shape against the unconstrained models.Signup table,
but a Postgres advisory transaction lock keyed on a hash of the email
serializes concurrent signups for the same address, so the second
request's SELECT only runs after the first has committed its INSERT and
sees it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models import Signup

app = FastAPI()


@app.post("/signups-locked")
async def create_signup_locked(email: str, session: AsyncSession):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email": email}
    )
    existing = (
        await session.execute(select(Signup).where(Signup.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": existing.id, "email": existing.email, "created": False}
    signup = Signup(email=email)
    session.add(signup)
    await session.commit()
    return {"id": signup.id, "email": signup.email, "created": True}
