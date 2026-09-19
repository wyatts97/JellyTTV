"""The built-in web player's playback endpoints.

The same stateful session engine the Jellyfin `/hls` proxy uses (ad detection,
backup substitution, coherent sequence numbering), served under a policy built
for a browser rather than for ffmpeg. hls.js absorbs a mid-stream resolution
change and honours `#EXT-X-DISCONTINUITY`, which is exactly what Jellyfin's
ffmpeg could not do - so here the stream can run at full quality and switch to
the never-stitched 360p source only for the length of an ad break.

Two modes, chosen per viewer in the player:

* `bridged` (default) - native quality; an ad break is covered by a clean
  backup, bridged first by `picture-by-picture`, and held on our black segment
  only while none has been found yet.
* `adfree` - `picture-by-picture` from the start, 360p, nothing to cover.

Every url handed to the browser is root-relative, so it resolves against
whatever origin the page was loaded from - `self_base_url` is where *Jellyfin*
reaches us, which a phone on the internet usually cannot.

Web sessions are keyed apart from Jellyfin's (`variant="web-<mode>"`), so the
two never share, advance or reset each other's sequence space.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.routers import hls as hls_router
from app.routers.hls import PlaybackPolicy
from app.security import AdminUser
from app.services import channels as channel_service
from app.services import stream_session
from app.services.settings_store import ResolvedSettings, get_settings
from app.util import iso_z, utcnow

router = APIRouter(prefix="/api/watch", tags=["watch"])

WatchMode = Literal["bridged", "adfree"]


def web_policy(settings: ResolvedSettings, mode: WatchMode) -> PlaybackPolicy:
    ad_free = mode == "adfree"
    return PlaybackPolicy(
        ad_free_source=ad_free,
        strip_ads=True,
        proxy_segments=settings.row.web_proxy_segments,
        hold=not ad_free,
        ad_spoofing=settings.row.ad_spoofing,
    )


def session_variant(mode: WatchMode) -> str:
    return f"web-{mode}"


def _segment_rewriter(login: str, proxy: bool):
    if not proxy:
        # hls.js fetches Twitch's CDN directly; its edges answer with
        # `Access-Control-Allow-Origin: *`.
        return lambda url: url

    def rewrite(url: str) -> str:
        return f"/api/watch/{quote(login)}/seg?u={hls_router.encode_url(url)}"

    return rewrite


def _hold_uri(login: str):
    def hold(seq: int) -> str:
        return f"/api/watch/{quote(login)}/hold?seq={seq}"

    return hold


@router.get("/{login}")
async def watch_info(
    login: str, _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    channel = await channel_service.get_channel_by_login(session, login)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"channel {login} is not tracked")
    cache_bust = int(utcnow().timestamp()) // 90
    return {
        "id": channel.id,
        "login": channel.twitch_login,
        "display_name": channel.display_name,
        "playable": channel.enabled and channel.live_enabled,
        "is_live": channel.is_live,
        "title": channel.live_title,
        "game": channel.live_game,
        "viewers": channel.live_viewers,
        "started_at": iso_z(channel.live_started_at),
        "avatar_url": f"/api/channels/{channel.id}/avatar",
        "thumbnail_url": f"/api/channels/{channel.id}/thumbnail?v={cache_bust}",
        "notify_enabled": channel.notify_enabled,
    }


@router.get("/{login}/live.m3u8", include_in_schema=False)
async def live_playlist(
    login: str,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    mode: Annotated[WatchMode, Query()] = "bridged",
) -> Response:
    settings = await get_settings(session)
    policy = web_policy(settings, mode)
    quality = await hls_router._channel_quality(session, settings, login, policy)
    return await hls_router._session_playlist(
        login=login,
        quality=quality,
        settings=settings,
        base="",
        key_suffix="",
        resolve=hls_router._make_resolver(login, quality, settings, policy),
        variant=session_variant(mode),
        policy=policy,
        rewrite_uri=_segment_rewriter(login, policy.proxy_segments),
        hold_uri=_hold_uri(login) if policy.hold else None,
    )


@router.get("/{login}/status")
async def live_status(
    login: str,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    mode: Annotated[WatchMode, Query()] = "bridged",
) -> dict:
    """What the session is doing right now, for the player's ad-break pill."""
    settings = await get_settings(session)
    policy = web_policy(settings, mode)
    quality = await hls_router._channel_quality(session, settings, login, policy)
    sess = stream_session.get(login, quality, session_variant(mode))
    if sess is None:
        return {"active": False, "mode": mode, "in_ad_break": False}
    snap = sess.snapshot()
    in_ad_break = bool(
        snap["serving_backup"] or snap["holding"] or snap["consecutive_ad_polls"] > 0
    )
    return {
        "active": True,
        "mode": mode,
        "in_ad_break": in_ad_break,
        "serving_backup": snap["serving_backup"],
        "serving_bridge": snap["serving_bridge"],
        "backup_player_type": snap["backup_player_type"] if snap["serving_backup"] else None,
        "backup_quality": snap["backup_quality"] if snap["serving_backup"] else None,
        "holding": snap["holding"],
        "stats": {
            "polls": snap["stats"]["polls"],
            "backup_polls": snap["stats"]["backup_polls"],
            "hold_segments": snap["stats"]["hold_segments"],
            "removed_segments": snap["stats"]["removed_segments"],
        },
    }


@router.get("/{login}/seg", include_in_schema=False)
async def segment(
    login: str,
    _user: AdminUser,
    u: Annotated[str, Query(description="Opaque upstream segment reference")],
) -> Response:
    url = hls_router.decode_url(u)
    stream_session.touch_any(login)
    return await hls_router.proxy_segment(url)


@router.get("/{login}/hold", include_in_schema=False)
async def hold(login: str, _user: AdminUser) -> Response:
    return hls_router.serve_hold(login)
