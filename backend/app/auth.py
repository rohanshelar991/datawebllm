from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Tuple


def hash_password(password: str, salt: str | None = None) -> str:
    salt_value = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value.encode("utf-8"), 120_000)
    return f"{salt_value}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, stored_digest = password_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hash_password(password, salt).split("$", 1)[1]
    return hmac.compare_digest(candidate, stored_digest)


def issue_session_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()
