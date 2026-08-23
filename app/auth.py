from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque


COOKIE_NAME = "charity_hub_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
_failed_attempts: dict[str, deque[float]] = defaultdict(deque)


def validate_runtime_config() -> None:
    if len(ADMIN_PASSWORD) < 12:
        raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
    if len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_raw, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_raw))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def check_password(username: str, password: str, stored_hash: str | None = None) -> bool:
    if not hmac.compare_digest(username, ADMIN_USERNAME):
        return False
    if stored_hash:
        return verify_password(password, stored_hash)
    return hmac.compare_digest(password, ADMIN_PASSWORD)


def _client_attempts(client_id: str) -> deque[float]:
    attempts = _failed_attempts[client_id]
    cutoff = time.time() - 600
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


def login_allowed(client_id: str) -> bool:
    return len(_client_attempts(client_id)) < 5


def record_failed_login(client_id: str) -> None:
    _client_attempts(client_id).append(time.time())


def clear_failed_logins(client_id: str) -> None:
    _failed_attempts.pop(client_id, None)


def create_session(username: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(12)
    payload = f"{username}:{expires}:{nonce}"
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_session(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split(":")
    if len(parts) != 4:
        return False
    username, expires, nonce, signature = parts
    payload = f"{username}:{expires}:{nonce}"
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        not_expired = int(expires) >= int(time.time())
    except ValueError:
        return False
    return not_expired and hmac.compare_digest(signature, expected) and hmac.compare_digest(username, ADMIN_USERNAME)
