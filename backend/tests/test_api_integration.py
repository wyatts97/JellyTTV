"""End-to-end tests through the real ASGI app and a real SQLite database.

These exercise the DB session layer, which the pure-unit tests do not touch.
Redis is not required: `enqueue()` and the event bus degrade to no-ops.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.services.twitch import HELIX_BASE, OAUTH_TOKEN_URL, OAUTH_VALIDATE_URL

BASE_URL = "http://testserver"

SETUP_PAYLOAD = {
    "username": "admin",
    "password": "supersecret123",
    "twitch_client_id": "cid",
    "twitch_client_secret": "csecret",
    "self_base_url": "http://jellyttv:8730",
}


@pytest.fixture
async def client():
    from app.db import dispose_engine, init_db
    from app.main import app

    await dispose_engine()
    await init_db()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        yield http
    await dispose_engine()


def _mock_twitch(mock: respx.MockRouter) -> None:
    """Let requests to our own ASGI app through; stub the Twitch endpoints."""
    mock.route(host="testserver").pass_through()
    mock.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 9000})
    )
    mock.get(OAUTH_VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"client_id": "cid", "expires_in": 9000})
    )
    mock.get(f"{HELIX_BASE}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "141981764",
                        "login": "twitchdev",
                        "display_name": "TwitchDev",
                        "profile_image_url": "https://cdn.example/avatar.png",
                        "offline_image_url": "https://cdn.example/offline.png",
                        "description": "Twitch developer tools",
                    }
                ]
            },
        )
    )
    mock.get(f"{HELIX_BASE}/streams").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "40952121085",
                        "user_id": "141981764",
                        "user_login": "twitchdev",
                        "title": "Twitch Developers Live",
                        "game_name": "Science & Technology",
                        "viewer_count": 175,
                        "started_at": "2026-02-01T15:00:00Z",
                        "thumbnail_url": "https://cdn.example/prev-%{width}x%{height}.jpg",
                    }
                ]
            },
        )
    )


async def _complete_setup(client: httpx.AsyncClient) -> dict:
    response = await client.post("/api/setup", json=SETUP_PAYLOAD)
    assert response.status_code == 200, response.text
    settings = await client.get("/api/settings")
    assert settings.status_code == 200, settings.text
    return settings.json()


# --------------------------------------------------------------------------- basics
async def test_health(client: httpx.AsyncClient):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_session_starts_unconfigured(client: httpx.AsyncClient):
    response = await client.get("/api/session")
    assert response.status_code == 200
    body = response.json()
    assert body["setup_complete"] is False
    # Pre-setup the API is open so the wizard can run.
    assert body["authenticated"] is True


async def test_setup_creates_admin_and_persists_settings(client: httpx.AsyncClient):
    settings = await _complete_setup(client)

    assert settings["setup_complete"] is True
    assert settings["admin_username"] == "admin"
    assert settings["twitch_client_id"] == "cid"
    # Secrets are never echoed back, only their presence.
    assert settings["twitch_client_secret_set"] is True
    assert "csecret" not in str(settings)
    assert settings["tuner_token"]
    assert settings["m3u_url"].startswith("http://jellyttv:8730/tuner/playlist.m3u?key=")
    assert settings["xmltv_url"].startswith("http://jellyttv:8730/tuner/guide.xml?key=")

    session = (await client.get("/api/session")).json()
    assert session["setup_complete"] is True
    assert session["authenticated"] is True


async def test_setup_cannot_run_twice(client: httpx.AsyncClient):
    await _complete_setup(client)
    again = await client.post("/api/setup", json=SETUP_PAYLOAD)
    assert again.status_code == 409


async def test_login_flow_and_wrong_password(client: httpx.AsyncClient):
    await _complete_setup(client)

    await client.post("/api/logout")
    bad = await client.post("/api/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401

    good = await client.post(
        "/api/login", json={"username": "admin", "password": "supersecret123"}
    )
    assert good.status_code == 200
    assert (await client.get("/api/session")).json()["authenticated"] is True


async def test_admin_api_requires_auth_after_setup(client: httpx.AsyncClient):
    await _complete_setup(client)
    await client.post("/api/logout")
    client.cookies.clear()

    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/channels")).status_code == 401
    # Health stays public for container healthchecks.
    assert (await client.get("/api/health")).status_code == 200


# ---------------------------------------------------------------------------- tuner
async def test_tuner_requires_the_key(client: httpx.AsyncClient):
    settings = await _complete_setup(client)
    token = settings["tuner_token"]

    await client.post("/api/logout")
    client.cookies.clear()

    assert (await client.get("/tuner/playlist.m3u")).status_code == 403
    assert (await client.get("/tuner/playlist.m3u?key=wrong")).status_code == 403

    ok = await client.get(f"/tuner/playlist.m3u?key={token}")
    assert ok.status_code == 200
    assert ok.text.startswith("#EXTM3U")


async def test_tuner_accepts_the_key_via_header(client: httpx.AsyncClient):
    settings = await _complete_setup(client)
    await client.post("/api/logout")
    client.cookies.clear()

    response = await client.get(
        "/tuner/playlist.m3u", headers={"X-JellyTTV-Key": settings["tuner_token"]}
    )
    assert response.status_code == 200


async def test_guide_is_valid_xml_when_empty(client: httpx.AsyncClient):
    settings = await _complete_setup(client)
    response = await client.get(f"/tuner/guide.xml?key={settings['tuner_token']}")
    assert response.status_code == 200
    assert "<tv " in response.text
    assert response.headers["content-type"].startswith("application/xml")


# -------------------------------------------------------------------------- channels
@respx.mock
async def test_add_channel_then_see_it_everywhere(client: httpx.AsyncClient):
    _mock_twitch(respx.mock)
    settings = await _complete_setup(client)
    token = settings["tuner_token"]

    created = await client.post("/api/channels", json={"channel": "https://twitch.tv/TwitchDev"})
    assert created.status_code == 201, created.text
    channel = created.json()

    assert channel["twitch_login"] == "twitchdev"
    assert channel["twitch_user_id"] == "141981764"
    assert channel["display_name"] == "TwitchDev"
    assert channel["tvg_id"] == "twitch.twitchdev"
    # Live payload from the mocked /streams response was applied.
    assert channel["is_live"] is True
    assert channel["live_title"] == "Twitch Developers Live"
    assert channel["live_viewers"] == 175
    # Thumbnail placeholders are expanded, not passed through raw.
    assert "%{width}" not in (channel["live_thumbnail_url"] or "")
    assert channel["stream_url"].endswith(f"/hls/twitchdev/master.m3u8?key={token}")

    listed = await client.get("/api/channels")
    assert listed.status_code == 200
    assert [c["twitch_login"] for c in listed.json()] == ["twitchdev"]

    playlist = (await client.get(f"/tuner/playlist.m3u?key={token}")).text
    assert 'tvg-id="twitch.twitchdev"' in playlist
    assert f"/hls/twitchdev/master.m3u8?key={token}" in playlist

    guide = (await client.get(f"/tuner/guide.xml?key={token}")).text
    assert '<channel id="twitch.twitchdev">' in guide
    assert "Twitch Developers Live" in guide

    dashboard = (await client.get("/api/dashboard")).json()
    assert dashboard["channels"]["total"] == 1
    assert dashboard["channels"]["live"] == 1
    assert dashboard["live"][0]["login"] == "twitchdev"


@respx.mock
async def test_duplicate_channel_is_rejected(client: httpx.AsyncClient):
    _mock_twitch(respx.mock)
    await _complete_setup(client)

    assert (await client.post("/api/channels", json={"channel": "twitchdev"})).status_code == 201
    duplicate = await client.post("/api/channels", json={"channel": "TwitchDev"})
    assert duplicate.status_code == 400
    assert "already being tracked" in duplicate.json()["detail"]


@respx.mock
async def test_unknown_channel_is_rejected(client: httpx.AsyncClient):
    mock = respx.mock
    mock.route(host="testserver").pass_through()
    mock.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 9000})
    )
    mock.get(f"{HELIX_BASE}/users").mock(return_value=httpx.Response(200, json={"data": []}))

    await _complete_setup(client)
    response = await client.post("/api/channels", json={"channel": "definitelynotreal"})
    assert response.status_code == 400
    assert "no channel called" in response.json()["detail"]


@respx.mock
async def test_update_and_delete_channel(client: httpx.AsyncClient):
    _mock_twitch(respx.mock)
    await _complete_setup(client)
    channel_id = (await client.post("/api/channels", json={"channel": "twitchdev"})).json()["id"]

    patched = await client.patch(
        f"/api/channels/{channel_id}",
        json={"vod_mode": "archive", "quality": "720p", "live_enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["vod_mode"] == "archive"
    assert patched.json()["quality"] == "720p"
    assert patched.json()["live_enabled"] is False

    # live_enabled=False removes it from the tuner but keeps the series.
    settings = (await client.get("/api/settings")).json()
    playlist = (await client.get(f"/tuner/playlist.m3u?key={settings['tuner_token']}")).text
    assert "twitch.twitchdev" not in playlist

    deleted = await client.delete(f"/api/channels/{channel_id}")
    assert deleted.status_code == 204
    assert (await client.get("/api/channels")).json() == []
    assert (await client.get(f"/api/channels/{channel_id}")).status_code == 404


# ------------------------------------------------------------------------ misc reads
async def test_empty_collections_and_diagnostics(client: httpx.AsyncClient):
    await _complete_setup(client)

    assert (await client.get("/api/vods")).json() == []
    assert (await client.get("/api/jobs")).json() == []
    assert (await client.get("/api/vods/summary")).json()["total"] == 0

    diagnostics = (await client.get("/api/diagnostics")).json()
    assert diagnostics["paths"]["media_root_writable"] is True
    assert "streamlink" in diagnostics["binaries"]
    assert diagnostics["urls"]["self_base_url"] == "http://jellyttv:8730"


async def test_settings_update_round_trip(client: httpx.AsyncClient):
    await _complete_setup(client)

    updated = await client.put(
        "/api/settings",
        json={"strip_ads": False, "default_quality": "720p", "guide_window_hours": 12},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["strip_ads"] is False
    assert body["default_quality"] == "720p"
    assert body["guide_window_hours"] == 12

    # Persisted, not just echoed.
    assert (await client.get("/api/settings")).json()["strip_ads"] is False


# --------------------------------------------------------------------- eventsub
async def test_eventsub_callback_rejects_bad_signatures(client: httpx.AsyncClient):
    await _complete_setup(client)

    response = await client.post(
        "/eventsub/callback",
        content=b'{"subscription":{"type":"stream.online"}}',
        headers={
            "Twitch-Eventsub-Message-Id": "msg-1",
            "Twitch-Eventsub-Message-Timestamp": "2026-01-01T00:00:00.000Z",
            "Twitch-Eventsub-Message-Signature": "sha256=deadbeef",
            "Twitch-Eventsub-Message-Type": "webhook_callback_verification",
        },
    )
    assert response.status_code == 403


async def test_eventsub_challenge_is_echoed_for_a_valid_signature(client: httpx.AsyncClient):
    import hashlib
    import hmac
    import json as jsonlib

    from app.db import session_scope
    from app.services.settings_store import get_settings
    from app.util import iso_z, utcnow

    await _complete_setup(client)

    async with session_scope() as session:
        secret = (await get_settings(session)).eventsub_secret
    assert secret

    body = jsonlib.dumps(
        {"challenge": "let-me-in", "subscription": {"type": "stream.online", "id": "s1"}}
    ).encode()
    message_id = "msg-verify-1"
    timestamp = iso_z(utcnow())
    signature = (
        "sha256="
        + hmac.new(
            secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
        ).hexdigest()
    )

    response = await client.post(
        "/eventsub/callback",
        content=body,
        headers={
            "Twitch-Eventsub-Message-Id": message_id,
            "Twitch-Eventsub-Message-Timestamp": timestamp,
            "Twitch-Eventsub-Message-Signature": signature,
            "Twitch-Eventsub-Message-Type": "webhook_callback_verification",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    # Twitch requires the raw challenge as the body, as text/plain.
    assert response.text == "let-me-in"
    assert response.headers["content-type"].startswith("text/plain")


async def test_rotating_the_tuner_token_changes_the_urls(client: httpx.AsyncClient):
    before = await _complete_setup(client)
    rotated = (await client.post("/api/settings/rotate-tuner-token")).json()

    assert rotated["tuner_token"] != before["tuner_token"]
    assert rotated["tuner_token"] in rotated["m3u_url"]

    # The old key must stop working.
    await client.post("/api/logout")
    client.cookies.clear()
    assert (await client.get(f"/tuner/playlist.m3u?key={before['tuner_token']}")).status_code == 403
