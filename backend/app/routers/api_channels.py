from __future__ import annotations

import base64
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_config
from app.db import get_db
from app.logging_conf import get_logger
from app.models import Channel, EventLog, EventSubSubscription, StreamSession, Vod, VodMode
from app.ratelimit import limiter
from app.schemas import ChannelCreate, ChannelOut, ChannelUpdate, VodOut
from app.security import AdminUser
from app.services import channels as channel_service
from app.services import library, vods
from app.services.channels import ChannelError
from app.services.settings_store import ResolvedSettings, get_settings
from app.util import sanitize_filename, twitch_thumbnail, utcnow
from app.worker.queue import enqueue

log = get_logger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

# 1x1 transparent PNG, served when an avatar is missing/unreachable so the
# frontend never has to render a browser's native broken-image icon.
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _to_out(channel: Channel, settings: ResolvedSettings, counts: dict[str, int]) -> ChannelOut:
    cfg = get_config()
    token = f"?key={settings.row.tuner_token}" if settings.row.tuner_token else ""
    return ChannelOut.build(
        channel,
        stream_url=f"{settings.self_base_url}/hls/{channel.twitch_login}/master.m3u8{token}",
        library_path=str(library.series_path(cfg.media_root, channel)),
        vod_counts=counts,
    )


async def _counts(session: AsyncSession, channel_id: int) -> dict[str, int]:
    rows = await vods.channel_vods(session, channel_id)
    counts: dict[str, int] = {}
    for row in rows:
        key = row.state.value
        counts[key] = counts.get(key, 0) + 1
    counts["total"] = len(rows)
    counts["archived_bytes"] = sum(r.bytes or 0 for r in rows)
    return counts


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> list[ChannelOut]:
    settings = await get_settings(session)
    rows = await channel_service.list_channels(session)
    return [_to_out(c, settings, await _counts(session, c.id or 0)) for c in rows]


@router.post("", response_model=ChannelOut, status_code=201)
async def create_channel(
    payload: ChannelCreate,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ChannelOut:
    settings = await get_settings(session)
    overrides = payload.model_dump(exclude={"channel"}, exclude_unset=True)
    try:
        channel = await channel_service.add_channel(
            session, settings, payload.channel, overrides=overrides
        )
    except ChannelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await enqueue("sync_vods", channel.id, job_id=f"sync_vods:{channel.id}:new")
    if settings.row.eventsub_enabled:
        await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")
    return _to_out(channel, settings, await _counts(session, channel.id or 0))


@router.get("/{channel_id}", response_model=ChannelOut)
async def read_channel(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ChannelOut:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    settings = await get_settings(session)
    return _to_out(channel, settings, await _counts(session, channel_id))


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int,
    payload: ChannelUpdate,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ChannelOut:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    values = payload.model_dump(exclude_unset=True)
    previous_dir = channel.series_dir
    previous_mode = channel.vod_mode
    previous_enabled = channel.enabled

    for key, value in values.items():
        setattr(channel, key, value)
    channel.updated_at = utcnow()
    session.add(channel)
    await session.commit()
    await session.refresh(channel)

    cfg = get_config()
    if previous_dir != channel.series_dir:
        # Move, never delete - the folder may hold gigabytes of archived VODs.
        moved = library.rename_series(cfg.media_root, previous_dir, channel.series_dir)
        if moved is not None:
            old_root = str(
                cfg.media_root / sanitize_filename(previous_dir, fallback=channel.twitch_login)
            )
            for row in await vods.channel_vods(session, channel_id):
                if row.file_path and row.file_path.startswith(old_root):
                    row.file_path = str(moved) + row.file_path[len(old_root):]
                    session.add(row)
            await session.commit()

    settings = await get_settings(session)
    if channel.vod_mode is not previous_mode or channel.enabled != previous_enabled:
        await enqueue("sync_vods", channel_id, job_id=f"sync_vods:{channel_id}:mode")
    await enqueue("publish_channel", channel_id, job_id=f"publish:{channel_id}")
    if settings.row.eventsub_enabled and channel.enabled != previous_enabled:
        await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")

    return _to_out(channel, settings, await _counts(session, channel_id))


@router.delete("/{channel_id}", status_code=204, response_class=Response)
async def delete_channel(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    delete_files: Annotated[bool, Query(description="Also delete the library folder")] = True,
) -> Response:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    settings = await get_settings(session)
    login = channel.twitch_login
    display = channel.display_name

    if delete_files:
        library.remove_series(get_config().media_root, channel)

    # Every table that references channel.id must be cleared first, or SQLite's
    # foreign key enforcement rejects the delete.
    for row in await vods.channel_vods(session, channel_id):
        await session.delete(row)
    for table in (StreamSession, EventSubSubscription):
        rows = (await session.exec(select(table).where(table.channel_id == channel_id))).all()
        for row in rows:
            await session.delete(row)
    await session.delete(channel)
    session.add(EventLog(category="channel", message=f"Removed channel {display} ({login})"))
    await session.commit()

    if settings.row.eventsub_enabled:
        await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")
    if settings.row.jellyfin_auto_refresh and settings.jellyfin_configured:
        await enqueue("jellyfin_refresh", job_id="jellyfin_refresh", defer_seconds=30)


@router.post("/{channel_id}/sync")
async def sync_channel(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    job_id = await enqueue(
        "sync_vods", channel_id, limit, job_id=f"sync_vods:{channel_id}:manual"
    )
    return {"queued": job_id is not None, "limit": limit}


@router.post("/{channel_id}/publish")
async def publish_channel(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    job_id = await enqueue("publish_channel", channel_id, job_id=f"publish:{channel_id}")
    return {"queued": job_id is not None}


@router.get("/{channel_id}/vods", response_model=list[VodOut])
async def channel_vods(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[VodOut]:
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    rows: list[Vod] = await vods.channel_vods(session, channel_id)
    return [VodOut.build(row, channel_login=channel.twitch_login) for row in rows]


@router.get("/{channel_id}/preview")
async def preview_paths(
    channel_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Show exactly what this channel will look like on disk."""
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    cfg = get_config()
    settings = await get_settings(session)
    rows = await vods.channel_vods(session, channel_id)
    sample = rows[0] if rows else None
    extension = ".mp4" if channel.vod_mode is VodMode.archive else ".strm"
    return {
        "series_path": str(library.series_path(cfg.media_root, channel)),
        "season_path": str(
            library.season_path(cfg.media_root, channel, sample.season if sample else 2026)
        ),
        "episode_file": (
            str(library.episode_path(cfg.media_root, channel, sample, extension))
            if sample
            else None
        ),
        "strm_contents": (
            library.build_strm_content(settings.self_base_url, sample, settings.row.tuner_token)
            if sample
            else None
        ),
    }


async def _fetch_image(url: str) -> httpx.Response | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return resp


@router.get("/{channel_id}/thumbnail", response_class=Response)
@limiter.limit("60/minute")
async def channel_thumbnail(
    request: Request,
    channel_id: int,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Live preview for a channel, falling back to the avatar.

    Never returns a broken image: if the live preview is missing or Twitch's CDN
    refuses it, serve the streamer's avatar with a 200 instead. Callers render
    the result directly, so a 404/502 here would surface as a broken card.
    """
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Re-normalise on read: rows written before the placeholder fix still hold a
    # literal `-{width}x{height}.jpg`, and this repairs them transparently.
    url = twitch_thumbnail(channel.live_thumbnail_url)
    resp = await _fetch_image(url) if url else None

    if resp is None:
        if url:
            log.debug(
                "live thumbnail unavailable, falling back to avatar",
                login=channel.twitch_login,
                url=url,
            )
        if not channel.avatar_url:
            raise HTTPException(status_code=404, detail="No thumbnail or avatar available")
        resp = await _fetch_image(channel.avatar_url)
        if resp is None:
            raise HTTPException(status_code=502, detail="failed to fetch thumbnail or avatar")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "max-age=30"},
    )


@router.get("/{channel_id}/avatar", response_class=Response)
@limiter.limit("60/minute")
async def channel_avatar(
    request: Request,
    channel_id: int,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Streamer avatar.

    Never returns a broken image: a missing or unreachable avatar serves a
    blank 1x1 PNG with a 200 instead, so the small avatar next to the
    streamer name never renders a browser's native broken-image icon.
    """
    channel = await channel_service.get_channel(session, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    resp = await _fetch_image(channel.avatar_url) if channel.avatar_url else None
    if resp is None:
        return Response(
            content=_BLANK_PNG,
            media_type="image/png",
            headers={"Cache-Control": "max-age=30"},
        )

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "max-age=300"},
    )
