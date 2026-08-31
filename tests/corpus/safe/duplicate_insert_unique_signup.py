"""safe / guard: unique_constraint

Pairs with vulnerable/duplicate_insert_signup.py, but writes into
models.UniqueSignup, whose email column is unique at the schema level.
The handler still does a check-then-insert, so a race is structurally
possible, but the loser's insert fails with an IntegrityError, which is
caught and reported as "already signed up" instead of creating a
duplicate row.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import UniqueSignup

app = FastAPI()


@app.post("/signups-guarded")
async def create_signup_guarded(email: str, session: AsyncSession):
    signup = UniqueSignup(email=email)
    session.add(signup)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {"email": email, "created": False}
    return {"id": signup.id, "email": email, "created": True}
