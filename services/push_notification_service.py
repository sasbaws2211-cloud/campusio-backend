"""Web Push notification sender — the push analog of services/sms_service.py's
SMSService: a thin client around one external capability (here, the Push
API via VAPID-signed requests instead of an SMS gateway), with the same
"fail gracefully if not configured, log and continue" posture so a missing
VAPID key never breaks a request that also happens to trigger a push.

pywebpush.webpush() is a synchronous (requests-based) call — run it off the
event loop via asyncio.to_thread so it can't block other requests.
"""
import asyncio
import base64
import binascii
import logging
from typing import Dict, List, Optional

from pywebpush import webpush, WebPushException
from py_vapid import Vapid02

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class PushNotificationService:
    def __init__(self):
        self._vapid: Optional[Vapid02] = None
        if settings.vapid_private_key_pem_b64:
            try:
                pem_bytes = base64.b64decode(settings.vapid_private_key_pem_b64)
                vapid = Vapid02()
                vapid.private_key = __import__("cryptography.hazmat.primitives.serialization", fromlist=["load_pem_private_key"]).load_pem_private_key(pem_bytes, password=None)
                self._vapid = vapid
            except Exception:
                logger.exception("Failed to load VAPID private key — push notifications disabled")

    @property
    def configured(self) -> bool:
        return self._vapid is not None and bool(settings.vapid_public_key)

    @property
    def public_key(self) -> Optional[str]:
        return settings.vapid_public_key

    def _send_sync(self, subscription_info: Dict, title: str, body: str, url: Optional[str]) -> None:
        import json
        payload = json.dumps({"title": title, "body": body, "url": url or "/"})
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=self._vapid,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=60 * 60 * 24,  # 24h — matches a reasonable "still relevant" window for a school-app ping
        )

    async def send(self, subscription: Dict, title: str, body: str, url: Optional[str] = None) -> Dict:
        """subscription: {"endpoint", "keys": {"p256dh", "auth"}}. Returns
        {"success": bool, "expired": bool, "error": str|None} — `expired`
        signals the caller (routers/push_notifications.py) should delete
        the subscription row, since a 404/410 from the push service means
        the browser unsubscribed or the endpoint rotated."""
        if not self.configured:
            return {"success": False, "expired": False, "error": "Push notifications are not configured (missing VAPID keys)"}
        try:
            await asyncio.to_thread(self._send_sync, subscription, title, body, url)
            return {"success": True, "expired": False, "error": None}
        except WebPushException as e:
            status_code = getattr(e.response, "status_code", None)
            expired = status_code in (404, 410)
            if not expired:
                logger.warning(f"Push send failed ({status_code}): {e}")
            return {"success": False, "expired": expired, "error": str(e)}
        except (binascii.Error, ValueError) as e:
            return {"success": False, "expired": True, "error": f"Malformed subscription: {e}"}

    async def send_to_many(self, subscriptions: List[Dict], title: str, body: str, url: Optional[str] = None) -> Dict:
        results = await asyncio.gather(*[self.send(s, title, body, url) for s in subscriptions])
        return {
            "sent": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"]),
            "expired_subscription_ids": [],  # caller maps results back to ids itself; see routers/push_notifications.py
        }


push_notification_service = PushNotificationService()
