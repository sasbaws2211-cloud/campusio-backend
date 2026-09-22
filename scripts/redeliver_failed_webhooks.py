#!/usr/bin/env python3
"""
Management Script: Retry Failed Webhook Deliveries

Usage:
    python scripts/redeliver_failed_webhooks.py                # Retry all eligible failed deliveries
    python scripts/redeliver_failed_webhooks.py --max-attempts=3

services/webhook_service.py delivers each event exactly once, synchronously,
via FastAPI BackgroundTasks — there is no in-process retry loop or durable
queue in this codebase (see run_billing_maintenance.py's docstring for the
same "no task scheduler" constraint). This script is the retry story:
find WebhookDelivery rows with status="failed" and attempts below the
cap, and retry each once. Run on whatever cadence makes sense (every few
minutes is typical) via an external scheduler.

Example crontab entry (every 5 minutes):
    */5 * * * * cd /path/to/campusio_backend && venv/bin/python scripts/redeliver_failed_webhooks.py >> logs/webhook_redelivery.log 2>&1
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlmodel import select

from database import async_session
from models.integrations import WebhookDelivery, WebhookEndpoint
from services.ai_key_crypto import decrypt_api_key
from services.webhook_service import DELIVERY_TIMEOUT_SECONDS, sign_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5


async def redeliver_failed_webhooks(max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.status == "failed",
                WebhookDelivery.attempts < max_attempts,
            )
        )
        deliveries = result.scalars().all()
        logger.info(f"Found {len(deliveries)} failed deliveries eligible for retry")

        for delivery in deliveries:
            endpoint = (
                await session.execute(
                    select(WebhookEndpoint).where(WebhookEndpoint.id == delivery.webhook_endpoint_id)
                )
            ).scalar_one_or_none()
            if endpoint is None or not endpoint.is_active:
                logger.info(f"Skipping delivery {delivery.id}: endpoint gone or inactive")
                continue

            body = delivery.payload.encode()
            try:
                secret = decrypt_api_key(endpoint.secret_encrypted)
                signature = sign_payload(secret, body)
                async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        endpoint.url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Campusio-Event": delivery.event_type,
                            "X-Campusio-Signature": signature,
                        },
                    )
                delivery.response_code = response.status_code
                delivery.status = "success" if response.is_success else "failed"
                logger.info(f"Retried delivery {delivery.id}: {delivery.status} ({response.status_code})")
            except Exception as e:
                delivery.status = "failed"
                logger.warning(f"Retry failed for delivery {delivery.id}: {e}")
            finally:
                delivery.attempts += 1
                delivery.last_attempted_at = datetime.utcnow()
                session.add(delivery)
                await session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retry failed webhook deliveries")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    args = parser.parse_args()
    asyncio.run(redeliver_failed_webhooks(max_attempts=args.max_attempts))
