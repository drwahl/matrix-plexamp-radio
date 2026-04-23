from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SESSION_DURATION = 30 * 24 * 3600  # 30 days
COOKIE_NAME = "radio_session"
_SECRET_FILE = "/data/session_secret"


def load_or_create_secret(configured: str) -> bytes:
    if configured:
        return configured.encode()
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "rb") as f:
            return f.read().strip()
    secret = os.urandom(32).hex().encode()
    with open(_SECRET_FILE, "wb") as f:
        f.write(secret)
    logger.info("Generated new session secret — stored in %s", _SECRET_FILE)
    return secret


def make_token(user_id: str, secret: bytes) -> str:
    expiry = int(time.time()) + SESSION_DURATION
    payload = f"{user_id}|{expiry}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_token(token: str, secret: bytes) -> Optional[str]:
    """Return the user_id if the token is valid and unexpired, else None."""
    try:
        # Split from the right twice — user_id (@user:server.com) contains colons but not pipes
        sig_sep = token.rfind("|")
        exp_sep = token.rfind("|", 0, sig_sep)
        user_id = token[:exp_sep]
        expiry = int(token[exp_sep + 1:sig_sep])
        sig = token[sig_sep + 1:]
        if time.time() > expiry:
            return None
        payload = token[:sig_sep]
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return user_id
    except Exception:
        return None


async def matrix_login(homeserver: str, username: str, password: str) -> Optional[str]:
    """Attempt a Matrix password login. Returns the user_id on success, None on failure."""
    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/login"
    body = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=body)
        if r.status_code == 200:
            return r.json().get("user_id")
        logger.warning("Matrix login rejected for %r: HTTP %s", username, r.status_code)
        return None
    except Exception:
        logger.exception("Matrix login request failed for %r", username)
        return None
