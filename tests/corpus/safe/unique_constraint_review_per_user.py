"""safe / guard: unique_constraint

Pairs with vulnerable/duplicate_insert_review_per_user.py, but writes
into models.Review, whose (user_id, product_id) pair carries a real
unique constraint. The handler still does a check-then-insert, so a race
is structurally possible, but Postgres rejects the loser's insert with
an IntegrityError instead of creating a duplicate row; the handler
catches that and treats it as "already reviewed" rather than crashing.
A reflected unique index covering the natural key is a full guard for
duplicate_insert per the guard table.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Review

app = FastAPI()


@app.post("/products/{product_id}/reviews-guarded")
async def create_review_guarded(
    product_id: int, user_id: int, rating: int, session: AsyncSession
):
    review = Review(user_id=user_id, product_id=product_id, rating=rating)
    session.add(review)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {"created": False, "reason": "already reviewed"}
    return {"id": review.id, "created": True}
