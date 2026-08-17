from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from app.services.eventsub import timestamp_is_fresh, verify_signature
from app.util import utcnow

SECRET = "a-secret-that-is-long-enough"


def _sign(message_id: str, timestamp: str, body: bytes, secret: str = SECRET) -> str:
    payload = message_id.encode() + timestamp.encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted():
    body = b'{"subscription":{"type":"stream.online"}}'
    message_id = "abc-123"
    timestamp = "2026-01-01T00:00:00.000Z"

    assert verify_signature(
        secret=SECRET,
        message_id=message_id,
        timestamp=timestamp,
        body=body,
        signature=_sign(message_id, timestamp, body),
    )


def test_tampered_body_is_rejected():
    message_id = "abc-123"
    timestamp = "2026-01-01T00:00:00.000Z"
    signature = _sign(message_id, timestamp, b'{"a":1}')

    assert not verify_signature(
        secret=SECRET,
        message_id=message_id,
        timestamp=timestamp,
        body=b'{"a":2}',
        signature=signature,
    )


def test_wrong_secret_is_rejected():
    body = b"{}"
    message_id = "id"
    timestamp = "2026-01-01T00:00:00.000Z"
    signature = _sign(message_id, timestamp, body, secret="different-secret-value")

    assert not verify_signature(
        secret=SECRET,
        message_id=message_id,
        timestamp=timestamp,
        body=body,
        signature=signature,
    )


def test_missing_pieces_are_rejected():
    assert not verify_signature(
        secret="", message_id="a", timestamp="b", body=b"", signature="sha256=x"
    )
    assert not verify_signature(
        secret=SECRET, message_id="", timestamp="b", body=b"", signature="sha256=x"
    )
    assert not verify_signature(
        secret=SECRET, message_id="a", timestamp="b", body=b"", signature=""
    )


def test_timestamp_freshness_window():
    now = utcnow()
    assert timestamp_is_fresh(now.isoformat() + "Z")
    assert timestamp_is_fresh((now - timedelta(minutes=9)).isoformat() + "Z")
    assert not timestamp_is_fresh((now - timedelta(minutes=11)).isoformat() + "Z")
    assert not timestamp_is_fresh("not-a-timestamp")
