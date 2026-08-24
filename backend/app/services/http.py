"""One shared, app-lifetime httpx client for all upstream Twitch traffic.

Playlist polling is the hot path: Jellyfin's ffmpeg re-requests a live playlist
every couple of seconds per active stream. Building a fresh `httpx.AsyncClient`
per request - which is what the proxy used to do - meant a full TLS handshake
each time, adding latency to exactly the requests that can least afford it.

The client is created lazily rather than only in the app lifespan, because the
test-suite drives the ASGI app through `ASGITransport` without ever running the
lifespan, and unit tests import routers directly.
"""

from __future__ import annotations

import httpx

# Twitch's CDN is friendlier to something that looks like the web player.
UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://player.twitch.tv",
    "Origin": "https://player.twitch.tv",
}

# Playlists are tiny and must never hold up the live edge; segments are large.
PLAYLIST_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0)
SEGMENT_TIMEOUT = httpx.Timeout(60.0, connect=5.0, read=60.0)
IMAGE_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0)

_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0,
)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the shared client, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=PLAYLIST_TIMEOUT,
            follow_redirects=True,
            limits=_LIMITS,
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Safe to call when it was never created."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
