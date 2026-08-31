"""duplicate_insert / high / duplicate_insert

"One review per user per product" enforced only in application logic
against models.PendingReview, which carries no unique constraint on
(user_id, product_id). Two concurrent review submissions from the same
user for the same product can both pass the existence check and both
insert, producing two reviews where the product page assumes at most
one. Compare with safe/unique_constraint_review_per_user.py, which uses
models.Review (constrained) instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PendingReview

app = FastAPI()


@app.post("/products/{product_id}/reviews")
async def create_review(
    product_id: int, user_id: int, rating: int, session: AsyncSession
):
    existing = (
        await session.execute(
            select(PendingReview).where(
                PendingReview.user_id == user_id,
                PendingReview.product_id == product_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.rating = rating
        await session.commit()
        return {"id": existing.id, "created": False}
    review = PendingReview(user_id=user_id, product_id=product_id, rating=rating)
    session.add(review)
    await session.commit()
    return {"id": review.id, "created": True}
