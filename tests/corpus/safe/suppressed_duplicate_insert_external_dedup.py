"""safe / guard: none recognised by the scanner table, suppressed

Webhook delivery is deduplicated upstream: the payment provider is
configured to send each event through a dedup proxy that rejects re-
delivery of the same external_id before it ever reaches this handler
(the proxy call is `_assert_not_seen_upstream`, an import into a service
the scanner has no visibility into). models.WebhookEvent itself carries
no unique constraint on external_id, and taken in isolation this handler
IS a duplicate_insert lookup-then-act race -- which is exactly why it is
suppressed rather than silently ignored: the guard is real but lives
entirely outside the code the scanner can read, so it is flagged low
confidence and explicitly suppressed with a named reason instead of
either being cleared automatically or reported at high confidence.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import WebhookEvent
from dedup_proxy import assert_not_seen_upstream  # external service, not in this repo

router = APIRouter(prefix="/webhooks")


@router.post("/payment-events")
async def handle_payment_event(external_id: str, session: AsyncSession):
    assert_not_seen_upstream(external_id)

    existing = (
        await session.execute(
            select(WebhookEvent).where(WebhookEvent.external_id == external_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"processed": False}  # raceguard: ignore[duplicate_insert] deduped upstream by the payment provider's delivery proxy before reaching this handler
    event = WebhookEvent(external_id=external_id, processed=True)
    session.add(event)
    await session.commit()
    return {"processed": True}
