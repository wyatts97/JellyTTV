"""`/stream/{login}.ts`: the HTTP contract Jellyfin's tuner depends on.

The streamlink process itself is covered in test_live_stream.py. These tests
fake it, and the database, so they can pin down status codes, content type, and
the one ordering rule that matters most here: the DB session is closed before
the first stream byte is sent.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.models import Settings
from app.routers import stream as stream_router
from app.services import live_stream, resolver
from app.services.settings_store import ResolvedSettings

TS = bytes([0x47]) + bytes(187)


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> FakeSession:
        self.log.append("session-open")
        return self

    async def __aexit__(self, *exc) -> None:
        self.log.append("session-closed")


class FakeHandle:
    def __init__(self, chunks: list[bytes], log: list[str]) -> None:
        self.chunks = chunks
        self.log = log

    async def iter_bytes(self):
        self.log.append("first-byte")
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def env(monkeypatch):
    """Wire the route to fakes. Returns the shared event log and a knob to
    choose what `open_stream` does."""
    log: list[str] = []
    state: dict = {"open": "stream", "opened": []}
    settings = ResolvedSettings(row=Settings(ad_free_source=True, twitch_device_id="dev123"))

    monkeypatch.setattr(stream_router, "get_session_factory", lambda: lambda: FakeSession(log))

    async def fake_check(session, request, key):
        if key != "good":
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Invalid or missing tuner key")

    async def fake_get_settings(session):
        return settings

    async def fake_quality(session, settings, login):
        return "best"

    async def fake_open(login, cmd):
        state["opened"].append((login, cmd))
        mode = state["open"]
        if mode == "offline":
            raise resolver.ChannelOffline(f"{login} is offline")
        if mode == "capacity":
            raise live_stream.StreamCapacityError("all 8 live stream slots are in use")
        if mode == "fail":
            raise live_stream.StreamStartError("streamlink exited before streaming: boom")
        return FakeHandle([TS * 10, TS * 10], log)

    monkeypatch.setattr(stream_router, "check_tuner_token", fake_check)
    monkeypatch.setattr(stream_router, "get_settings", fake_get_settings)
    monkeypatch.setattr(stream_router, "_channel_quality", fake_quality)
    monkeypatch.setattr(stream_router.live_stream, "open_stream", fake_open)

    app = FastAPI()
    app.include_router(stream_router.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, log, state


async def test_a_live_channel_streams_as_mpeg_ts(env):
    client, _log, _state = env
    response = await client.get("/stream/alpha.ts?key=good")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"
    assert "no-store" in response.headers["cache-control"]
    assert response.content == TS * 20


async def test_the_db_session_is_closed_before_the_first_byte_is_sent(env):
    """A live stream runs for hours. Holding a DB session that long would pin
    the SQLite pool and stall every other request in the app."""
    client, log, _state = env
    await client.get("/stream/alpha.ts?key=good")

    assert log.index("session-closed") < log.index("first-byte")


async def test_head_answers_without_starting_streamlink(env):
    """Jellyfin probes with HEAD; that must not spawn a process."""
    client, _log, state = env
    response = await client.head("/stream/alpha.ts?key=good")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp2t"
    assert state["opened"] == []


async def test_the_ad_free_player_type_and_device_id_reach_streamlink(env):
    client, _log, state = env
    await client.get("/stream/alpha.ts?key=good")

    [(login, cmd)] = state["opened"]
    assert login == "alpha"
    assert "--stdout" in cmd
    assert f"playerType={resolver.AD_FREE_PLAYER_TYPE}" in cmd
    assert "X-Device-Id=dev123" in cmd
    assert cmd[-2:] == ["https://www.twitch.tv/alpha", "best"]
    # Deprecated in streamlink 8 and slated for removal - an unknown argument
    # would stop every stream from starting.
    assert "--twitch-disable-ads" not in cmd


async def test_an_offline_channel_is_a_503_with_a_long_retry(env):
    client, _log, state = env
    state["open"] = "offline"
    response = await client.get("/stream/alpha.ts?key=good")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"


async def test_a_full_house_is_a_503(env):
    client, _log, state = env
    state["open"] = "capacity"
    response = await client.get("/stream/alpha.ts?key=good")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


async def test_a_failed_start_is_a_503_with_a_short_retry(env):
    client, _log, state = env
    state["open"] = "fail"
    response = await client.get("/stream/alpha.ts?key=good")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "boom" in response.json()["detail"]


async def test_a_bad_tuner_key_is_refused_before_anything_starts(env):
    client, _log, state = env
    response = await client.get("/stream/alpha.ts?key=wrong")

    assert response.status_code == 403
    assert state["opened"] == []
