"""The built-in web player's playback endpoints."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.models import Channel, Settings
from app.routers import hls as hls_router
from app.routers import watch as watch_router
from app.services import resolver, stream_session
from app.services.settings_store import ResolvedSettings

BASE_URL = "http://testserver"
SETUP_PAYLOAD = {
    "username": "admin",
    "password": "supersecret123",
    "twitch_client_id": "cid",
    "twitch_client_secret": "csecret",
    "self_base_url": "http://jellyttv:8730",
}
NATIVE_URL = "https://video-weaver.a.hls.ttvnw.net/v1/playlist/native.m3u8"
ADFREE_URL = "https://video-weaver.b.hls.ttvnw.net/v1/playlist/pbyp.m3u8"


def media_playlist(tag: str, start: int = 100, count: int = 6) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{start}"]
    for seq in range(start, start + count):
        lines.append("#EXTINF:2.000,live")
        lines.append(f"https://video-edge-1.abc.hls.ttvnw.net/v1/segment/{tag}{seq}.ts")
    return "\n".join(lines) + "\n"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch):
    from app.db import dispose_engine, init_db, session_scope
    from app.main import app

    stream_session.reset()
    await dispose_engine()
    await init_db()
    async with session_scope() as session:
        session.add(
            Channel(
                twitch_login="adapt",
                twitch_user_id="1",
                display_name="Adapt",
                series_dir="Adapt",
                quality="1080p60",
                is_live=True,
                live_title="ALASKA MARATHON",
            )
        )
        await session.commit()

    resolved: list[dict] = []

    async def fake_resolve_live(login: str, **kwargs):
        resolved.append({"login": login, **kwargs})
        return ADFREE_URL if kwargs.get("player_type") == resolver.AD_FREE_PLAYER_TYPE else NATIVE_URL

    async def fake_fetch(url: str):
        return 200, media_playlist("pbyp" if url == ADFREE_URL else "native")

    monkeypatch.setattr(resolver, "resolve_live", fake_resolve_live)
    monkeypatch.setattr(hls_router, "_fetch_playlist", fake_fetch)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        http.resolved = resolved  # type: ignore[attr-defined]
        assert (await http.post("/api/setup", json=SETUP_PAYLOAD)).status_code == 200
        yield http
    stream_session.reset()
    await dispose_engine()


async def test_the_player_requires_a_signed_in_admin(client: httpx.AsyncClient):
    await client.post("/api/logout")
    client.cookies.clear()
    assert (await client.get("/api/watch/adapt/live.m3u8")).status_code == 401
    assert (await client.get("/api/watch/adapt")).status_code == 401


async def test_bridged_mode_plays_native_quality_with_direct_cdn_segments(client):
    response = await client.get("/api/watch/adapt/live.m3u8")
    assert response.status_code == 200
    assert "native100.ts" in response.text
    # Straight to Twitch's CDN - never `self_base_url`, which is Jellyfin's view.
    assert "jellyttv:8730" not in response.text
    call = client.resolved[-1]
    assert call["quality"] == "1080p60"
    assert call["player_type"] != resolver.AD_FREE_PLAYER_TYPE


async def test_adfree_mode_uses_the_never_stitched_source(client):
    response = await client.get("/api/watch/adapt/live.m3u8", params={"mode": "adfree"})
    assert response.status_code == 200
    assert "pbyp100.ts" in response.text
    call = client.resolved[-1]
    assert call["player_type"] == resolver.AD_FREE_PLAYER_TYPE
    assert call["quality"] == "best"


async def test_web_sessions_are_keyed_apart_from_jellyfin_sessions(client):
    await client.get("/api/watch/adapt/live.m3u8")
    await client.get("/api/watch/adapt/live.m3u8", params={"mode": "adfree"})
    keys = {s["login"] + ":" + str(s["quality"]) for s in stream_session.stats()}
    assert len(stream_session.stats()) == 2, keys
    assert stream_session.get("adapt", "1080p60", "web-bridged") is not None
    assert stream_session.get("adapt", "best", "web-adfree") is not None
    # Nothing was created under the key the Jellyfin proxy uses.
    assert stream_session.get("adapt", "1080p60") is None


async def test_proxied_segments_use_root_relative_urls(client):
    from app.db import session_scope

    async with session_scope() as session:
        row = await session.get(Settings, 1)
        row.web_proxy_segments = True
        session.add(row)
        await session.commit()

    text = (await client.get("/api/watch/adapt/live.m3u8")).text
    assert "\n/api/watch/adapt/seg?u=" in text


async def test_status_reports_an_idle_then_active_session(client):
    idle = (await client.get("/api/watch/adapt/status")).json()
    assert idle["active"] is False
    await client.get("/api/watch/adapt/live.m3u8")
    status = (await client.get("/api/watch/adapt/status")).json()
    assert status["active"] is True
    assert status["in_ad_break"] is False


async def test_info_describes_the_channel(client):
    info = (await client.get("/api/watch/adapt")).json()
    assert info["display_name"] == "Adapt"
    assert info["is_live"] is True
    assert info["avatar_url"].startswith("/api/channels/")
    assert (await client.get("/api/watch/nobody")).status_code == 404


def test_web_policies():
    settings = ResolvedSettings(row=Settings(ad_free_source=True, strip_ads=False))
    bridged = watch_router.web_policy(settings, "bridged")
    # Independent of the Jellyfin settings: the browser can take the switch.
    assert bridged.ad_free_source is False
    assert bridged.strip_ads is True
    assert bridged.hold is True
    adfree = watch_router.web_policy(settings, "adfree")
    assert adfree.ad_free_source is True
    assert adfree.hold is False
    assert hls_router._make_backup_finder("adapt", settings, adfree) is None
    assert hls_router._make_backup_finder("adapt", settings, bridged) is not None
