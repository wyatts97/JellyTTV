"""Twitch Helix + OAuth client.

App access tokens are obtained with the client_credentials grant. They live for
~60 days and are *not* refreshable, so we cache them in-process, validate them
periodically, and simply request a new one on 401.

Docs: https://dev.twitch.tv/docs/api/reference
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.logging_conf import get_logger

log = get_logger(__name__)

OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
OAUTH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
HELIX_BASE = "https://api.twitch.tv/helix"

EVENTSUB_TYPES: tuple[tuple[str, str], ...] = (
    ("stream.online", "1"),
    ("stream.offline", "1"),
    ("channel.update", "2"),
)


class TwitchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class TwitchAuthError(TwitchError):
    pass


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    @property
    def valid(self) -> bool:
        # Refresh 5 minutes early.
        return bool(self.value) and time.time() < self.expires_at - 300


@dataclass
class _TokenStore:
    tokens: dict[str, _CachedToken] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get(self, client_id: str) -> _CachedToken | None:
        token = self.tokens.get(client_id)
        return token if token and token.valid else None

    def set(self, client_id: str, value: str, expires_in: int) -> None:
        self.tokens[client_id] = _CachedToken(value=value, expires_at=time.time() + expires_in)

    def invalidate(self, client_id: str) -> None:
        self.tokens.pop(client_id, None)


_token_store = _TokenStore()


class TwitchClient:
    """Thin async Helix wrapper. Construct per unit of work; cheap to create."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise TwitchAuthError("Twitch client id and secret are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self._timeout = timeout
        self._external_client = client
        self._client: httpx.AsyncClient | None = client

    # ---------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> TwitchClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # -------------------------------------------------------------------- auth
    async def app_token(self, *, force: bool = False) -> str:
        if force:
            _token_store.invalidate(self.client_id)
        cached = _token_store.get(self.client_id)
        if cached:
            return cached.value

        async with _token_store.lock:
            cached = _token_store.get(self.client_id)
            if cached:
                return cached.value

            response = await self._http().post(
                OAUTH_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            if response.status_code != 200:
                raise TwitchAuthError(
                    "Twitch rejected the client credentials",
                    status=response.status_code,
                    body=response.text[:500],
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise TwitchAuthError("Twitch did not return an access token")
            _token_store.set(self.client_id, token, int(payload.get("expires_in", 3600)))
            log.info("obtained twitch app access token", expires_in=payload.get("expires_in"))
            return token

    async def validate_token(self) -> dict[str, Any]:
        token = await self.app_token()
        response = await self._http().get(
            OAUTH_VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}
        )
        if response.status_code != 200:
            _token_store.invalidate(self.client_id)
            raise TwitchAuthError("app access token is no longer valid", status=response.status_code)
        return response.json()

    # ----------------------------------------------------------------- request
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_body: dict[str, Any] | None = None,
        _retries: int = 2,
    ) -> dict[str, Any]:
        token = await self.app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id,
        }
        url = path if path.startswith("http") else f"{HELIX_BASE}{path}"

        response = await self._http().request(
            method, url, params=params, json=json_body, headers=headers
        )

        if response.status_code == 401 and _retries > 0:
            log.warning("twitch returned 401, refreshing app token")
            await self.app_token(force=True)
            return await self._request(
                method, path, params=params, json_body=json_body, _retries=_retries - 1
            )

        if response.status_code == 429 and _retries > 0:
            reset = response.headers.get("Ratelimit-Reset")
            delay = 1.0
            if reset:
                try:
                    delay = max(0.5, min(30.0, float(reset) - time.time()))
                except ValueError:
                    delay = 1.0
            log.warning("twitch rate limited, backing off", seconds=round(delay, 2))
            await asyncio.sleep(delay)
            return await self._request(
                method, path, params=params, json_body=json_body, _retries=_retries - 1
            )

        if response.status_code == 204 or not response.content:
            return {}

        if response.status_code >= 400:
            raise TwitchError(
                f"Twitch {method} {path} failed with {response.status_code}",
                status=response.status_code,
                body=response.text[:500],
            )

        return response.json()

    # ------------------------------------------------------------------ helpers
    async def get_users(
        self, *, logins: list[str] | None = None, ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = []
        for login in (logins or [])[:100]:
            params.append(("login", login.lower()))
        for user_id in (ids or [])[:100]:
            params.append(("id", user_id))
        if not params:
            return []
        payload = await self._request("GET", "/users", params=params)
        return payload.get("data", [])

    async def get_user(self, login: str) -> dict[str, Any] | None:
        users = await self.get_users(logins=[login])
        return users[0] if users else None

    async def get_streams(self, user_ids: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for chunk_start in range(0, len(user_ids), 100):
            chunk = user_ids[chunk_start : chunk_start + 100]
            params: list[tuple[str, Any]] = [("user_id", uid) for uid in chunk]
            params.append(("first", 100))
            payload = await self._request("GET", "/streams", params=params)
            results.extend(payload.get("data", []))
        return results

    async def get_videos(
        self,
        user_id: str,
        *,
        video_type: str = "archive",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(collected) < limit:
            page_size = min(100, limit - len(collected))
            params: dict[str, Any] = {
                "user_id": user_id,
                "type": video_type,
                "first": page_size,
                "sort": "time",
            }
            if cursor:
                params["after"] = cursor
            payload = await self._request("GET", "/videos", params=params)
            data = payload.get("data", [])
            collected.extend(data)
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not data or not cursor:
                break
        return collected[:limit]

    async def get_video(self, video_id: str) -> dict[str, Any] | None:
        payload = await self._request("GET", "/videos", params={"id": video_id})
        data = payload.get("data", [])
        return data[0] if data else None

    async def get_games(self, game_ids: list[str]) -> list[dict[str, Any]]:
        ids = [g for g in game_ids if g][:100]
        if not ids:
            return []
        payload = await self._request("GET", "/games", params=[("id", g) for g in ids])
        return payload.get("data", [])

    # ----------------------------------------------------------------- eventsub
    async def list_eventsub_subscriptions(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"after": cursor} if cursor else None
            payload = await self._request("GET", "/eventsub/subscriptions", params=params)
            collected.extend(payload.get("data", []))
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return collected

    async def create_eventsub_subscription(
        self,
        *,
        sub_type: str,
        version: str,
        condition: dict[str, Any],
        callback: str,
        secret: str,
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/eventsub/subscriptions",
            json_body={
                "type": sub_type,
                "version": version,
                "condition": condition,
                "transport": {"method": "webhook", "callback": callback, "secret": secret},
            },
        )
        data = payload.get("data", [])
        return data[0] if data else {}

    async def delete_eventsub_subscription(self, sub_id: str) -> None:
        await self._request("DELETE", "/eventsub/subscriptions", params={"id": sub_id})
