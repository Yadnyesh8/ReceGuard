"""duplicate_insert / high / duplicate_insert

Classic "create if it does not exist" signup flow: look up by email, and
if nothing is found, insert a new row. The `signups` table (see
models.Signup) carries no unique constraint on email, so two concurrent
signups with the same address can both miss the lookup and both insert,
producing two rows for one email.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Signup

app = FastAPI()


@app.post("/signups")
async def create_signup(email: str, session: AsyncSession):
    existing = (
        await session.execute(select(Signup).where(Signup.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": existing.id, "email": existing.email, "created": False}
    signup = Signup(email=email)
    session.add(signup)
    await session.commit()
    return {"id": signup.id, "email": signup.email, "created": True}
