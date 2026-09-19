"""Go-live Web Push from the PWA."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.crypto import decrypt, encrypt
from app.models import Channel, PushSubscription, Settings
from app.services import notifications, webpush
from app.services.settings_store import ResolvedSettings

BASE_URL = "http://testserver"
SETUP_PAYLOAD = {
    "username": "admin",
    "password": "supersecret123",
    "twitch_client_id": "cid",
    "twitch_client_secret": "csecret",
}
SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "BPk3", "auth": "a1b2"},
    "label": "Chrome on Android",
}


class FakePush:
    """Stands in for pywebpush.webpush, answering per endpoint."""

    def __init__(self) -> None:
        self.status: dict[str, int] = {}
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        code = self.status.get(kwargs["subscription_info"]["endpoint"], 201)
        response = httpx.Response(code)
        if code >= 400:
            raise webpush.WebPushException("push failed", response=response)
        return response


@pytest.fixture
def fake_push(monkeypatch: pytest.MonkeyPatch) -> FakePush:
    fake = FakePush()
    monkeypatch.setattr(webpush, "webpush", fake)
    return fake


@pytest.fixture
async def client():
    from app.db import dispose_engine, init_db
    from app.main import app

    await dispose_engine()
    await init_db()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as http:
        assert (await http.post("/api/setup", json=SETUP_PAYLOAD)).status_code == 200
        yield http
    await dispose_engine()


async def test_a_vapid_key_pair_is_generated_once_and_kept(client):
    from app.db import session_scope

    first = (await client.get("/api/push/key")).json()["public_key"]
    second = (await client.get("/api/push/key")).json()["public_key"]
    assert first == second
    raw = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert len(raw) == 65 and raw[0] == 0x04  # uncompressed P-256 point

    async with session_scope() as session:
        row = await session.get(Settings, 1)
        # Encrypted at rest, and not the plain value.
        private = decrypt(row.vapid_private_key_enc)
        assert private and private not in row.vapid_private_key_enc


async def test_subscribe_is_an_upsert_by_endpoint(client):
    assert (await client.post("/api/push/subscriptions", json=SUB)).status_code == 201
    updated = {**SUB, "keys": {"p256dh": "NEW", "auth": "NEW"}}
    assert (await client.post("/api/push/subscriptions", json=updated)).status_code == 201
    subs = (await client.get("/api/push/subscriptions")).json()
    assert len(subs) == 1
    assert subs[0]["label"] == "Chrome on Android"


async def test_a_non_https_endpoint_is_refused(client):
    bad = {**SUB, "endpoint": "http://192.168.1.1/admin"}
    assert (await client.post("/api/push/subscriptions", json=bad)).status_code == 422


async def test_unsubscribe(client):
    await client.post("/api/push/subscriptions", json=SUB)
    response = await client.request(
        "DELETE", "/api/push/subscriptions", json={"endpoint": SUB["endpoint"]}
    )
    assert response.status_code == 204
    assert (await client.get("/api/push/subscriptions")).json() == []


async def test_test_push_reports_delivery_and_prunes_gone_devices(client, fake_push):
    await client.post("/api/push/subscriptions", json=SUB)
    gone = {**SUB, "endpoint": "https://updates.push.services.mozilla.com/wpush/v2/gone"}
    await client.post("/api/push/subscriptions", json=gone)
    fake_push.status[gone["endpoint"]] = 410

    result = (await client.post("/api/push/test")).json()
    assert result["ok"] is True
    assert result["message"] == "Sent to 1 of 2 device(s)"
    subs = (await client.get("/api/push/subscriptions")).json()
    assert [s["endpoint"] for s in subs] == [SUB["endpoint"]]

    call = fake_push.calls[0]
    assert call["vapid_claims"]["sub"].startswith(("mailto:", "https://"))
    assert call["ttl"] == webpush.TTL_SECONDS


async def test_repeated_failures_drop_a_subscription(client, fake_push):
    from app.db import session_scope
    from app.services.settings_store import get_settings

    await client.post("/api/push/subscriptions", json=SUB)
    fake_push.status[SUB["endpoint"]] = 500
    for _ in range(webpush.MAX_FAILURES):
        async with session_scope() as session:
            await webpush.send_all(session, await get_settings(session), {"title": "x"})
    assert (await client.get("/api/push/subscriptions")).json() == []


# --------------------------------------------------------------- notify_live
def make_channel(**kwargs) -> Channel:
    defaults = {
        "id": 7,
        "twitch_login": "adapt",
        "twitch_user_id": "1",
        "display_name": "Adapt",
        "series_dir": "Adapt",
        "is_live": True,
        "live_title": "ALASKA MARATHON",
        "live_game": "Just Chatting",
    }
    defaults.update(kwargs)
    return Channel(**defaults)


async def _subscribed_settings(**kwargs) -> ResolvedSettings:
    from app.db import session_scope
    from app.services.settings_store import get_settings

    async with session_scope() as session:
        settings = await get_settings(session)
        for key, value in kwargs.items():
            setattr(settings.row, key, value)
        session.add(settings.row)
        session.add(PushSubscription(endpoint=SUB["endpoint"], p256dh="k", auth="a"))
        await session.commit()
        await session.refresh(settings.row)
        return settings


@respx.mock
async def test_go_live_fans_out_to_web_push_and_streamyfin(client, fake_push):
    from app.db import session_scope

    route = respx.post("http://jellyfin:8096/Streamyfin/notification").mock(
        return_value=httpx.Response(204)
    )
    settings = await _subscribed_settings(
        notify_on_live=True,
        jellyfin_url="http://jellyfin:8096",
        jellyfin_api_key_enc=encrypt("k"),
    )
    async with session_scope() as session:
        assert await notifications.notify_live(settings, make_channel(), session) is True

    assert route.called
    payload = json.loads(fake_push.calls[0]["data"])
    assert payload["title"] == "Adapt is live"
    assert payload["url"] == "/watch/adapt"
    assert payload["tag"] == "live-adapt"
    assert payload["icon"] == "/api/channels/7/avatar"


async def test_web_push_works_without_jellyfin(client, fake_push):
    from app.db import session_scope

    settings = await _subscribed_settings(notify_on_live=False)
    async with session_scope() as session:
        assert await notifications.notify_live(settings, make_channel(), session) is True
    assert len(fake_push.calls) == 1


async def test_a_muted_channel_notifies_nobody(client, fake_push):
    from app.db import session_scope

    settings = await _subscribed_settings()
    async with session_scope() as session:
        sent = await notifications.notify_live(
            settings, make_channel(notify_enabled=False), session
        )
    assert sent is False
    assert fake_push.calls == []


async def test_web_push_can_be_switched_off(client, fake_push):
    from app.db import session_scope

    settings = await _subscribed_settings(webpush_enabled=False)
    async with session_scope() as session:
        assert await notifications.notify_live(settings, make_channel(), session) is False
    assert fake_push.calls == []
