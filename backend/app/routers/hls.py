"""HLS proxy.

Jellyfin (really ffmpeg) is pointed at `/hls/{login}/master.m3u8`. We resolve the
real Twitch playlist with streamlink, strip stitched ad segments, and hand back a
rewritten playlist. Segments are 302-redirected to Twitch's CDN by default so we
do not pay the bandwidth cost; set `proxy_segments` to stream them through us
instead (useful when the Jellyfin host cannot reach Twitch directly).

The stable, never-expiring url is ours - the short-lived Twitch token stays
hidden behind it, so Jellyfin never gets a dead playlist mid-session.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.logging_conf import get_logger
from app.security import require_tuner_token
from app.services import channels as channel_service
from app.services import hls, resolver
from app.services.settings_store import ResolvedSettings, get_settings

log = get_logger(__name__)
router = APIRouter(tags=["stream"], dependencies=[Depends(require_tuner_token)])

PLAYLIST_MEDIA_TYPE = "application/vnd.apple.mpegurl"
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://player.twitch.tv",
    "Origin": "https://player.twitch.tv",
}

ALLOWED_UPSTREAM_SUFFIXES = (
    ".ttvnw.net",
    ".twitch.tv",
    ".twitchcdn.net",
    ".cloudfront.net",
    ".akamaized.net",
    ".hls.ttvnw.net",
)


def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_url(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        url = base64.urlsafe_b64decode(token + padding).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="malformed upstream reference") from exc
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="unsupported upstream scheme")
    host = urlsplit(url).hostname or ""
    if not host.endswith(ALLOWED_UPSTREAM_SUFFIXES):
        # Prevents this proxy being used as an open relay.
        raise HTTPException(status_code=403, detail="upstream host not allowed")
    return url


def _key_suffix(request: Request) -> str:
    key = request.query_params.get("key")
    return f"&key={quote(key)}" if key else ""


def _base_from_request(request: Request, settings: ResolvedSettings) -> str:
    configured = settings.self_base_url
    if configured and not configured.startswith("http://localhost"):
        return configured
    parts = urlsplit(str(request.url))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=UPSTREAM_HEADERS)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream playlist error: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"upstream playlist returned {response.status_code}"
        )
    return response.text


async def _resolve_channel(
    session: AsyncSession, settings: ResolvedSettings, login: str
) -> tuple[str, str]:
    channel = await channel_service.get_channel_by_login(session, login)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"channel {login} is not tracked")
    if not channel.enabled or not channel.live_enabled:
        raise HTTPException(status_code=409, detail=f"{channel.display_name} is disabled")
    try:
        upstream = await resolver.resolve_live(
            channel.twitch_login,
            quality=channel.quality or settings.row.default_quality,
            user_token=settings.twitch_user_token,
        )
    except resolver.ChannelOffline as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{channel.display_name} is offline",
        ) from exc
    except resolver.ResolveError as exc:
        log.warning("stream resolve failed", login=login, error=str(exc))
        raise HTTPException(status_code=502, detail=f"could not resolve stream: {exc}") from exc
    return upstream, channel.quality or "best"


def _segment_rewriter(base: str, login: str, key_suffix: str, proxy_segments: bool):
    if not proxy_segments:
        return None

    def rewrite(url: str) -> str:
        return f"{base}/hls/{quote(login)}/seg?u={encode_url(url)}{key_suffix}"

    return rewrite


@router.api_route("/hls/{login}/master.m3u8", methods=["GET", "HEAD"], include_in_schema=False)
async def master_playlist(
    login: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    settings = await get_settings(session)
    upstream, _quality = await _resolve_channel(session, settings, login)

    if not settings.row.proxy_enabled:
        # Redirect-only mode: cheapest, but no ad stripping.
        return RedirectResponse(upstream, status_code=status.HTTP_302_FOUND)

    base = _base_from_request(request, settings)
    key_suffix = _key_suffix(request)
    playlist = await _fetch_text(upstream)

    if hls.is_master_playlist(playlist):
        def to_media(variant_url: str) -> str:
            return (
                f"{base}/hls/{quote(login)}/media.m3u8?u={encode_url(variant_url)}{key_suffix}"
            )

        result = hls.rewrite_master(playlist, upstream, rewrite_uri=to_media)
        return Response(result.text, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE)

    # streamlink usually hands us a media playlist directly - serve it as-is.
    result = hls.rewrite_playlist(
        playlist,
        upstream,
        strip_ads=settings.row.strip_ads,
        rewrite_uri=_segment_rewriter(base, login, key_suffix, settings.row.proxy_segments),
    )
    _log_strip(login, result)
    return Response(result.text, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE)


@router.api_route("/hls/{login}/media.m3u8", methods=["GET", "HEAD"], include_in_schema=False)
async def media_playlist(
    login: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    u: Annotated[str | None, Query(description="Opaque upstream playlist reference")] = None,
) -> Response:
    settings = await get_settings(session)

    if u:
        upstream = decode_url(u)
    else:
        upstream, _quality = await _resolve_channel(session, settings, login)

    base = _base_from_request(request, settings)
    key_suffix = _key_suffix(request)
    playlist = await _fetch_text(upstream)

    result = hls.rewrite_playlist(
        playlist,
        upstream,
        strip_ads=settings.row.strip_ads,
        rewrite_uri=_segment_rewriter(base, login, key_suffix, settings.row.proxy_segments),
    )
    _log_strip(login, result)
    return Response(result.text, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE)


@router.api_route("/hls/{login}/seg", methods=["GET", "HEAD"], include_in_schema=False)
async def segment(
    login: str,
    u: Annotated[str, Query(description="Opaque upstream segment reference")],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    settings = await get_settings(session)
    url = decode_url(u)

    if not settings.row.proxy_segments:
        return RedirectResponse(url, status_code=status.HTTP_302_FOUND)

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True)
    try:
        upstream_request = client.build_request("GET", url, headers=UPSTREAM_HEADERS)
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"segment fetch failed: {exc}") from exc

    if response.status_code >= 400:
        code = response.status_code
        await response.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"segment upstream returned {code}")

    async def body():
        try:
            async for chunk in response.aiter_bytes(65536):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type=response.headers.get("content-type", "video/mp2t"),
        headers={"Cache-Control": "no-store"},
    )


@router.api_route("/vod/{video_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def vod_stream(
    video_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Target of the .strm files we write, so Twitch links never go stale."""
    settings = await get_settings(session)
    try:
        upstream = await resolver.resolve_vod(
            video_id,
            quality=settings.row.default_quality,
            user_token=settings.twitch_user_token,
        )
    except resolver.ResolveError as exc:
        log.warning("vod resolve failed", video_id=video_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not resolve VOD {video_id}: {exc}",
        ) from exc
    return RedirectResponse(upstream, status_code=status.HTTP_302_FOUND)


def _log_strip(login: str, result: hls.PlaylistResult) -> None:
    if result.removed_segments:
        log.info(
            "stripped twitch ad segments",
            login=login,
            segments=result.removed_segments,
            seconds=result.removed_seconds,
        )
