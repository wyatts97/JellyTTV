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

import asyncio
import base64
import binascii
import hashlib
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
from app.services import ad_events, adblock, hls, resolver, stream_session
from app.services import http as shared_http
from app.services.http import UPSTREAM_HEADERS
from app.services.settings_store import ResolvedSettings, get_settings

log = get_logger(__name__)
router = APIRouter(tags=["stream"], dependencies=[Depends(require_tuner_token)])

PLAYLIST_MEDIA_TYPE = "application/vnd.apple.mpegurl"

# Hard ceiling on how long a client may be kept waiting for a playlist. ffmpeg -
# which is what the Jellyfin web client is really driving - abandons a live
# stream that answers slowly, so a late playlist is worse than a stale one. The
# session already avoids blocking (its backup search is detached), and this is
# the backstop that keeps any future work on that path from regressing it.
PLAYLIST_DEADLINE_SECONDS = 6.0
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

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


async def _fetch_playlist(url: str) -> tuple[int, str]:
    """Fetch a playlist, reporting transport failures as status 0.

    The session decides what a given status means - a 403 means the weaver url
    is dead and must be re-resolved, a 502 is worth retrying against the same
    url - so this must not collapse everything into one exception.
    """
    try:
        response = await shared_http.get_client().get(url, headers=UPSTREAM_HEADERS)
    except httpx.HTTPError as exc:
        log.debug("upstream playlist transport error", url=url, error=str(exc))
        return 0, ""
    return response.status_code, response.text


async def _fetch_text(url: str) -> str:
    """Strict single-shot fetch, for callers with no session to fall back on."""
    status_code, text = await _fetch_playlist(url)
    if status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"upstream playlist returned {status_code}"
        )
    return text


async def _channel_quality(
    session: AsyncSession, settings: ResolvedSettings, login: str
) -> str:
    """Validate the channel is tracked and playable, returning its quality."""
    channel = await channel_service.get_channel_by_login(session, login)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"channel {login} is not tracked")
    if not channel.enabled or not channel.live_enabled:
        raise HTTPException(status_code=409, detail=f"{channel.display_name} is disabled")
    return channel.quality or settings.row.default_quality or "best"


def _make_resolver(login: str, quality: str, settings: ResolvedSettings):
    """Build the callable a stream session uses to (re)acquire an upstream url.

    Passed in rather than called up front so the session controls *when* a new
    weaver url is fetched: re-resolving mid-playback hands back a different host
    whose numbering does not line up with what the player already buffered.
    """
    calls = {"n": 0}

    async def resolve(prefer_direct: bool = False) -> str:
        calls["n"] += 1
        # `prefer_direct` is the session telling us an ad arrived over the route
        # we last used, so mint the next token from the other side of the proxy.
        proxy = None if prefer_direct else resolver.configured_proxy(
            settings.row.twitch_proxy_url
        )
        try:
            return await resolver.resolve_live(
                login,
                quality=quality,
                user_token=settings.twitch_user_token,
                player_type=settings.row.twitch_player_type,
                proxy_url=proxy,
                # The first resolve of a session may use the cache; every
                # subsequent one is a recovery attempt and must be fresh.
                force=calls["n"] > 1,
            )
        except resolver.ChannelOffline as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{login} is offline",
            ) from exc
        except resolver.ResolveError as exc:
            log.warning("stream resolve failed", login=login, error=str(exc))
            raise HTTPException(
                status_code=502, detail=f"could not resolve stream: {exc}"
            ) from exc

    return resolve


def _make_backup_finder(login: str, settings: ResolvedSettings):
    """Build the backup search, or None when the strategy does not use one.

    Only the TTV-AB strategy substitutes a backup stream. Under `ttv_lol_pro`
    the ad is meant to never exist (the token is minted through an ad-free
    region), and under `strip_only` an ad pod is simply not served - so in both
    cases going and fetching another player type would be wasted work.
    """
    strategy = adblock.configured_strategy(settings.row.ad_block_strategy)
    if strategy != adblock.STRATEGY_TTV_AB or not settings.row.strip_ads:
        return None

    async def find(state: adblock.BackupState, quality: str):
        return await adblock.find_backup(
            login=login,
            quality=quality,
            native_player_type=resolver.resolve_player_type(
                settings.row.twitch_player_type
            ),
            state=state,
            fetch=_fetch_playlist,
            user_token=settings.twitch_user_token,
            allow_low_quality=settings.row.ad_backup_low_quality,
        )

    return find


def _make_ad_reporter(settings: ResolvedSettings):
    """Build the ad-progress reporter, or None when spoofing is disabled."""
    if not settings.row.ad_spoofing:
        return None

    async def report(playlist: str) -> int:
        return await ad_events.report_blocked_ads(
            playlist, user_token=settings.twitch_user_token
        )

    return report


async def _resolve_channel(
    session: AsyncSession, settings: ResolvedSettings, login: str
) -> tuple[str, str]:
    """Resolve immediately. Only for the non-session paths (redirect mode, VODs)."""
    quality = await _channel_quality(session, settings, login)
    upstream = await _make_resolver(login, quality, settings)()
    return upstream, quality


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
    quality = await _channel_quality(session, settings, login)
    resolve = _make_resolver(login, quality, settings)

    if not settings.row.proxy_enabled:
        # Redirect-only mode: cheapest, but no ad stripping.
        return RedirectResponse(await resolve(), status_code=status.HTTP_302_FOUND)

    base = _base_from_request(request, settings)
    key_suffix = _key_suffix(request)

    # streamlink hands back a single variant, so this endpoint almost always
    # serves a *media* playlist despite its name. Check once per session and
    # remember, rather than re-fetching to re-decide on every poll.
    existing = await stream_session.ensure(login, quality)
    if existing.is_master is not False:
        upstream = await resolve()
        probe = await _fetch_text(upstream)
        # Recorded either way. Leaving it None on a master (which is what used
        # to happen - only `_session_playlist` ever wrote the flag, and only
        # False) meant a master-serving channel re-probed on every single
        # request, and any non-200 on that probe became a hard 502.
        existing.is_master = hls.is_master_playlist(probe)
        if existing.is_master:
            def to_media(variant_url: str) -> str:
                return (
                    f"{base}/hls/{quote(login)}/media.m3u8"
                    f"?u={encode_url(variant_url)}{key_suffix}"
                )

            result = hls.rewrite_master(probe, upstream, rewrite_uri=to_media)
            return Response(result.text, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE)

    return await _session_playlist(
        login=login,
        quality=quality,
        settings=settings,
        base=base,
        key_suffix=key_suffix,
        resolve=resolve,
    )


async def _session_playlist(
    *,
    login: str,
    quality: str,
    settings: ResolvedSettings,
    base: str,
    key_suffix: str,
    resolve,
    variant: str | None = None,
) -> Response:
    """Serve a media playlist through the stateful session."""
    try:
        render = await asyncio.wait_for(
            stream_session.get_playlist(
                login=login,
                quality=quality,
                strip_ads=settings.row.strip_ads,
                resolve=resolve,
                fetch=_fetch_playlist,
                rewrite_uri=_segment_rewriter(
                    base, login, key_suffix, settings.row.proxy_segments
                ),
                variant=variant,
                backup=_make_backup_finder(login, settings),
                report_ads=_make_ad_reporter(settings),
            ),
            timeout=PLAYLIST_DEADLINE_SECONDS,
        )
    except TimeoutError:
        # Re-serving the last playlist is legal HLS and reads as "nothing new
        # yet", which is far better than making the client wait any longer.
        stale = stream_session.get(login, quality, variant)
        log.warning(
            "playlist render exceeded its deadline",
            login=login,
            seconds=PLAYLIST_DEADLINE_SECONDS,
            served_stale=bool(stale and stale.last_rendered),
        )
        if stale is not None and stale.last_rendered:
            return Response(
                stale.last_rendered, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="playlist timed out",
            headers={"Retry-After": "1"},
        ) from None
    except stream_session.SessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc

    sess = stream_session.get(login, quality, variant)
    if sess is not None:
        sess.is_master = False

    if render.removed_segments:
        log.info(
            "stripped twitch ad segments",
            login=login,
            segments=render.removed_segments,
            media_sequence=render.media_sequence,
            discontinuity_sequence=render.discontinuity_sequence,
            backup=render.backup_player_type,
        )
    return Response(render.text, media_type=PLAYLIST_MEDIA_TYPE, headers=NO_CACHE)


@router.api_route("/hls/{login}/media.m3u8", methods=["GET", "HEAD"], include_in_schema=False)
async def media_playlist(
    login: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    u: Annotated[str | None, Query(description="Opaque upstream playlist reference")] = None,
) -> Response:
    settings = await get_settings(session)
    quality = await _channel_quality(session, settings, login)

    if u:
        # A pinned variant of a real master playlist. It gets its own session so
        # each variant keeps a separate sequence space.
        pinned = decode_url(u)
        variant = hashlib.sha1(u.encode()).hexdigest()[:12]

        async def resolve(prefer_direct: bool = False) -> str:
            # A pinned variant has no token of its own to re-mint, so the
            # polarity flip has nothing to act on here.
            return pinned
    else:
        variant = None
        resolve = _make_resolver(login, quality, settings)

    return await _session_playlist(
        login=login,
        quality=quality,
        settings=settings,
        base=_base_from_request(request, settings),
        key_suffix=_key_suffix(request),
        resolve=resolve,
        variant=variant,
    )


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

    # A client pulling segments is alive even if a playlist poll runs late.
    # Must use the channel's own quality, not the global default: they are
    # different session keys, and touching the wrong one let a session with a
    # per-channel quality override get swept out from under a live client.
    stream_session.touch(login, await _channel_quality(session, settings, login))

    client = shared_http.get_client()
    try:
        upstream_request = client.build_request(
            "GET", url, headers=UPSTREAM_HEADERS, timeout=shared_http.SEGMENT_TIMEOUT
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"segment fetch failed: {exc}") from exc

    if response.status_code >= 400:
        code = response.status_code
        await response.aclose()
        raise HTTPException(status_code=502, detail=f"segment upstream returned {code}")

    async def body():
        # Only the response is closed here: the client is shared and long-lived,
        # so closing it would tear down the connection pool for everyone.
        try:
            async for chunk in response.aiter_bytes(65536):
                yield chunk
        finally:
            await response.aclose()

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
            player_type=settings.row.twitch_player_type,
        )
    except resolver.ResolveError as exc:
        log.warning("vod resolve failed", video_id=video_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not resolve VOD {video_id}: {exc}",
        ) from exc
    return RedirectResponse(upstream, status_code=status.HTTP_302_FOUND)
