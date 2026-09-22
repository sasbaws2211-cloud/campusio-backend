"""Outbound webhook delivery (see models/integrations.py).

Signing mirrors services/paystack_service.py's `verify_webhook_signature`
exactly (HMAC-SHA512 hex digest) so the codebase has one signature
convention for both directions — a receiver of a Campusio webhook verifies
it the same way Campusio itself verifies Paystack's.

Delivery is fire-and-forget via FastAPI's BackgroundTasks (the codebase's
existing "side effect after responding" pattern, e.g. routers/students.py's
add_parent) — there is no task queue in this codebase, and this feature
isn't the one to introduce it. Reliability comes from the WebhookDelivery
audit row plus scripts/redeliver_failed_webhooks.py, not an in-request
retry loop.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import List

import httpx
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from database import async_session
from models.integrations import WebhookDelivery, WebhookEndpoint
from services.ai_key_crypto import decrypt_api_key

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT_SECONDS = 5.0


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(key=secret.encode(), msg=body, digestmod=hashlib.sha512).hexdigest()


async def emit_event(
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    school_id: str,
    event_type: str,
    payload: dict,
) -> None:
    """Schedules delivery to every active endpoint this school has
    subscribed to `event_type`. Never raises — a webhook subscriber being
    unreachable must not affect the request that triggered the event."""
    try:
        result = await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.school_id == school_id,
                WebhookEndpoint.is_active == True,  # noqa: E712
            )
        )
        endpoints = [e for e in result.scalars().all() if event_type in e.subscribed_events]
    except Exception as e:
        logger.error(f"Failed to look up webhook endpoints for {event_type}: {e}")
        return

    for endpoint in endpoints:
        background_tasks.add_task(_deliver, endpoint.id, event_type, payload)


async def _deliver(webhook_endpoint_id: str, event_type: str, payload: dict) -> None:
    """Runs in the background, after the triggering request has already
    responded — uses its own DB session, since the request's session may
    already be closed by the time this executes."""
    body = json.dumps({"event": event_type, "data": payload}, default=str).encode()

    async with async_session() as session:
        endpoint = (
            await session.execute(select(WebhookEndpoint).where(WebhookEndpoint.id == webhook_endpoint_id))
        ).scalar_one_or_none()
        if endpoint is None or not endpoint.is_active:
            return

        delivery = WebhookDelivery(
            webhook_endpoint_id=endpoint.id,
            event_type=event_type,
            payload=body.decode(),
        )

        try:
            secret = decrypt_api_key(endpoint.secret_encrypted)
            signature = sign_payload(secret, body)
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    endpoint.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Campusio-Event": event_type,
                        "X-Campusio-Signature": signature,
                    },
                )
            delivery.response_code = response.status_code
            delivery.status = "success" if response.is_success else "failed"
        except Exception as e:
            logger.warning(f"Webhook delivery failed for endpoint {endpoint.id} ({event_type}): {e}")
            delivery.status = "failed"
        finally:
            delivery.attempts = 1
            delivery.last_attempted_at = datetime.utcnow()
            session.add(delivery)
            await session.commit()
