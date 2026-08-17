from __future__ import annotations

import httpx
import pytest
import respx

from app.services.twitch import (
    HELIX_BASE,
    OAUTH_TOKEN_URL,
    TwitchAuthError,
    TwitchClient,
    _token_store,
)

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _token_store.tokens.clear()
    yield
    _token_store.tokens.clear()


def _token_route(mock: respx.MockRouter, token: str = "app-token-1", expires_in: int = 5000):
    return mock.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": token, "expires_in": expires_in, "token_type": "bearer"}
        )
    )


@respx.mock
async def test_app_token_is_cached():
    route = _token_route(respx.mock)
    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        first = await client.app_token()
        second = await client.app_token()

    assert first == second == "app-token-1"
    assert route.call_count == 1


@respx.mock
async def test_bad_credentials_raise_auth_error():
    respx.mock.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"message": "invalid client"})
    )
    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        with pytest.raises(TwitchAuthError):
            await client.app_token()


def test_missing_credentials_raise_immediately():
    with pytest.raises(TwitchAuthError):
        TwitchClient("", "")


@respx.mock
async def test_get_user_sends_required_headers():
    _token_route(respx.mock)
    route = respx.mock.get(f"{HELIX_BASE}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "141981764",
                        "login": "twitchdev",
                        "display_name": "TwitchDev",
                        "profile_image_url": "https://cdn/avatar.png",
                        "offline_image_url": "https://cdn/offline.png",
                        "description": "Twitch developer tools",
                    }
                ]
            },
        )
    )

    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        user = await client.get_user("TwitchDev")

    assert user is not None
    assert user["id"] == "141981764"
    request = route.calls[0].request
    assert request.headers["Client-Id"] == CLIENT_ID
    assert request.headers["Authorization"] == "Bearer app-token-1"
    assert "login=twitchdev" in str(request.url)


@respx.mock
async def test_expired_token_triggers_one_refresh_then_succeeds():
    token_route = respx.mock.post(OAUTH_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "stale", "expires_in": 5000}),
            httpx.Response(200, json={"access_token": "fresh", "expires_in": 5000}),
        ]
    )
    users_route = respx.mock.get(f"{HELIX_BASE}/users").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Invalid OAuth token"}),
            httpx.Response(200, json={"data": [{"id": "1", "login": "x", "display_name": "X"}]}),
        ]
    )

    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        user = await client.get_user("x")

    assert user is not None
    assert token_route.call_count == 2
    assert users_route.call_count == 2
    assert users_route.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_get_streams_chunks_and_flattens():
    _token_route(respx.mock)
    respx.mock.get(f"{HELIX_BASE}/streams").mock(
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
                        "thumbnail_url": "https://cdn/preview-%{width}x%{height}.jpg",
                    }
                ]
            },
        )
    )

    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        streams = await client.get_streams(["141981764"])

    assert len(streams) == 1
    assert streams[0]["viewer_count"] == 175


@respx.mock
async def test_get_videos_follows_pagination_until_limit():
    _token_route(respx.mock)
    respx.mock.get(f"{HELIX_BASE}/videos").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "1", "title": "a", "duration": "1h0m0s"},
                        {"id": "2", "title": "b", "duration": "30m0s"},
                    ],
                    "pagination": {"cursor": "next-page"},
                },
            ),
            httpx.Response(
                200,
                json={"data": [{"id": "3", "title": "c", "duration": "15m0s"}], "pagination": {}},
            ),
        ]
    )

    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        videos = await client.get_videos("141981764", limit=10)

    assert [v["id"] for v in videos] == ["1", "2", "3"]


@respx.mock
async def test_eventsub_subscription_payload_shape():
    _token_route(respx.mock)
    route = respx.mock.post(f"{HELIX_BASE}/eventsub/subscriptions").mock(
        return_value=httpx.Response(
            202,
            json={
                "data": [
                    {
                        "id": "sub-1",
                        "status": "webhook_callback_verification_pending",
                        "type": "stream.online",
                    }
                ]
            },
        )
    )

    async with TwitchClient(CLIENT_ID, CLIENT_SECRET) as client:
        created = await client.create_eventsub_subscription(
            sub_type="stream.online",
            version="1",
            condition={"broadcaster_user_id": "141981764"},
            callback="https://example.com/eventsub/callback",
            secret="secret-value-1234567890",
        )

    assert created["id"] == "sub-1"
    body = route.calls[0].request.content.decode()
    assert '"method":"webhook"' in body.replace(" ", "")
    assert '"callback":"https://example.com/eventsub/callback"' in body.replace(" ", "")
