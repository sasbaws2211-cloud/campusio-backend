"""API key generation, hashing, and resolution (see models/integrations.py).

Keys are high-entropy random tokens, not user-chosen passwords, so a fast
hash (SHA-256) is the correct primitive here — unlike auth.py's bcrypt use
for user passwords, there's no brute-force-guessing risk to slow down.
"""
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.integrations import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "campusio_live_"


def generate_key() -> Tuple[str, str]:
    """Returns (raw_key, key_prefix). The raw key is shown to the caller
    exactly once at creation time and is never recoverable afterward."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[:len(KEY_PREFIX) + 4]


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


@dataclass
class ApiKeyContext:
    api_key_id: str
    school_id: str
    scopes: List[str]


async def verify_and_resolve(raw_key: str, session: AsyncSession) -> Optional[ApiKeyContext]:
    """Resolves a raw API key to its scoped context, or None if invalid,
    revoked, or expired. Never raises — callers turn None into a 401."""
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    key = result.scalar_one_or_none()
    if key is None or not key.is_active:
        return None
    if key.expires_at and key.expires_at < datetime.utcnow():
        return None

    try:
        key.last_used_at = datetime.utcnow()
        session.add(key)
        await session.commit()
    except Exception as e:
        logger.warning(f"Failed to update api_key.last_used_at: {e}")

    return ApiKeyContext(api_key_id=key.id, school_id=key.school_id, scopes=key.scopes)
