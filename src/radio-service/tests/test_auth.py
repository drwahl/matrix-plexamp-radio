from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.auth import SESSION_DURATION, make_token, verify_token, load_or_create_secret


def test_roundtrip():
    secret = b"test-secret"
    user_id = "@alice:example.com"
    assert verify_token(make_token(user_id, secret), secret) == user_id


def test_wrong_secret():
    token = make_token("@alice:example.com", b"secret-a")
    assert verify_token(token, b"secret-b") is None


def test_tampered_signature():
    secret = b"s"
    token = make_token("@alice:example.com", secret)
    assert verify_token(token[:-4] + "xxxx", secret) is None


def test_tampered_payload():
    secret = b"s"
    token = make_token("@alice:example.com", secret)
    # Replace the user portion — signature will no longer match
    tampered = "@eve:evil.com" + token[len("@alice:example.com"):]
    assert verify_token(tampered, secret) is None


def test_expired():
    secret = b"s"
    user_id = "@alice:example.com"
    # Build a valid token with an already-past expiry
    expiry = int(time.time()) - 1
    payload = f"{user_id}|{expiry}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    token = f"{payload}|{sig}"
    assert verify_token(token, secret) is None


def test_malformed_no_pipes():
    assert verify_token("no-separators-at-all", b"s") is None


def test_malformed_empty():
    assert verify_token("", b"s") is None


def test_user_id_with_colons():
    # Matrix user IDs contain colons — make sure splitting logic handles them
    secret = b"s"
    user_id = "@bot.radio:drwahl.me"
    assert verify_token(make_token(user_id, secret), secret) == user_id


def test_load_or_create_secret_uses_configured():
    assert load_or_create_secret("my-secret") == b"my-secret"


def test_load_or_create_secret_stable_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("app.auth._SECRET_FILE", str(tmp_path / "secret"))
    s1 = load_or_create_secret("")
    s2 = load_or_create_secret("")
    assert s1 == s2
    assert len(s1) > 0
