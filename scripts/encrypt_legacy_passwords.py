#!/usr/bin/env python3
"""One-time backfill: re-encrypt every User.plain_text_password row that
still holds genuine plaintext (written before routers/auth.py, staff.py,
and students.py started encrypting it — see auth.py's
encrypt_onboarding_password/decrypt_onboarding_password).

Safe to run more than once — a row that decrypts successfully is already
encrypted and is left untouched; only a row that fails to decrypt (i.e. is
still plaintext) gets re-written.

Usage:
    python scripts/encrypt_legacy_passwords.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import select

from database import async_session
from models.user import User
from services.ai_key_crypto import decrypt_api_key, encrypt_api_key


async def encrypt_legacy_passwords() -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.plain_text_password.is_not(None))
        )
        users = result.scalars().all()

        migrated = 0
        already_encrypted = 0
        for user in users:
            try:
                decrypt_api_key(user.plain_text_password)
                already_encrypted += 1
                continue
            except Exception:
                pass  # genuine plaintext — migrate it below

            user.plain_text_password = encrypt_api_key(user.plain_text_password)
            session.add(user)
            migrated += 1

        if migrated:
            await session.commit()

        print(f"Checked {len(users)} user(s) with a stored password: {migrated} encrypted, {already_encrypted} already encrypted.")


if __name__ == "__main__":
    asyncio.run(encrypt_legacy_passwords())
