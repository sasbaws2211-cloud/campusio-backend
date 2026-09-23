"""Symmetric encryption for BYOK API keys at rest.

Derives a Fernet key from the app's existing secret_key env var (already
used for JWT signing — see auth.py) via SHA-256, so no new secret needs
to be provisioned. Never log or return a decrypted key.
"""
import base64
import hashlib
import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    secret = os.environ.get("secret_key", "")
    if not secret:
        raise RuntimeError("secret_key is not configured")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_api_key(raw_key: str) -> str:
    return _get_fernet().encrypt(raw_key.encode()).decode()


def decrypt_api_key(encrypted_key: str) -> str:
    return _get_fernet().decrypt(encrypted_key.encode()).decode()
