from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import __version__
from app.config import get_config
from app.db import get_db
from app.models import EventLog, Job, JobState
from app.security import AdminUser
from app.services import channels as channel_service
from app.services import events as event_bus
from app.services import eventsub as eventsub_service
from app.services import library, resolver, vods
from app.services.settings_store import get_settings
from app.util import iso_z

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/live")
async def live_channels(
    session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """Public endpoint returning live channel data for the Jellyfin plugin.

    No auth required — this only exposes public info (login, title, game, viewers,
    thumbnail/avatar proxy URLs) and is meant to be consumed by the companion
    Jellyfin plugin's proxy controller.
    """
    rows = await channel_service.list_channels(session)
    live = [c for c in rows if c.is_live and c.enabled and c.live_enabled]
    return {
        "channels": [
            {
                "id": c.id,
                "login": c.twitch_login,
                "display_name": c.display_name,
                "is_live": True,
                "title": c.live_title,
                "game_name": c.live_game,
                "viewer_count": c.live_viewers,
                "thumbnail_url": f"/api/channels/{c.id}/thumbnail" if c.live_thumbnail_url else None,
                "avatar_url": f"/api/channels/{c.id}/avatar" if c.avatar_url else None,
                "started_at": iso_z(c.live_started_at),
            }
            for c in live
        ]
    }


@router.get("/dashboard")
async def dashboard(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    cfg = get_config()
    settings = await get_settings(session)
    rows = await channel_service.list_channels(session)
    live = [c for c in rows if c.is_live and c.enabled and c.live_enabled]

    active_jobs = list(
        (
            await session.exec(
                select(Job)
                .where(Job.state.in_([JobState.queued, JobState.running]))  # type: ignore[attr-defined]
                .order_by(Job.created_at.desc())
                .limit(20)
            )
        ).all()
    )
    recent_logs = list(
        (await session.exec(select(EventLog).order_by(EventLog.created_at.desc()).limit(25))).all()
    )

    return {
        "version": __version__,
        "channels": {
            "total": len(rows),
            "enabled": sum(1 for c in rows if c.enabled),
            "live": len(live),
        },
        "live": [
            {
                "id": c.id,
                "login": c.twitch_login,
                "display_name": c.display_name,
                "title": c.live_title,
                "game": c.live_game,
                "viewers": c.live_viewers,
                "started_at": iso_z(c.live_started_at),
                "thumbnail_url": c.live_thumbnail_url,
                "avatar_url": c.avatar_url,
            }
            for c in live
        ],
        "vods": await vods.count_by_state(session),
        "disk": library.disk_usage(cfg.media_root),
        "jobs": [
            {
                "id": j.id,
                "type": j.type,
                "key": j.key,
                "state": j.state.value,
                "progress": j.progress,
                "message": j.message,
                "created_at": iso_z(j.created_at),
            }
            for j in active_jobs
        ],
        "logs": [
            {
                "id": entry.id,
                "level": entry.level,
                "category": entry.category,
                "message": entry.message,
                "created_at": iso_z(entry.created_at),
            }
            for entry in recent_logs
        ],
        "eventsub": {
            "enabled": settings.row.eventsub_enabled,
            "possible": settings.eventsub_possible,
            "mode": (
                "webhook"
                if settings.row.eventsub_enabled and settings.eventsub_possible
                else "polling"
            ),
            **await eventsub_service.subscription_health(session),
        },
        "setup": {
            "twitch": settings.twitch_configured,
            "jellyfin": settings.jellyfin_configured,
        },
    }


@router.get("/jobs")
async def list_jobs(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = list(
        (await session.exec(select(Job).order_by(Job.created_at.desc()).limit(limit))).all()
    )
    return [
        {
            "id": j.id,
            "type": j.type,
            "key": j.key,
            "state": j.state.value,
            "progress": j.progress,
            "message": j.message,
            "created_at": iso_z(j.created_at),
            "started_at": iso_z(j.started_at),
            "finished_at": iso_z(j.finished_at),
        }
        for j in rows
    ]


@router.get("/logs")
async def list_logs(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = list(
        (
            await session.exec(select(EventLog).order_by(EventLog.created_at.desc()).limit(limit))
        ).all()
    )
    return [
        {
            "id": e.id,
            "level": e.level,
            "category": e.category,
            "message": e.message,
            "channel_id": e.channel_id,
            "created_at": iso_z(e.created_at),
        }
        for e in rows
    ]


@router.get("/diagnostics")
async def diagnostics(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    cfg = get_config()
    settings = await get_settings(session)
    media_root = cfg.media_root
    writable = False
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        probe = media_root / ".jellyttv-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
    except OSError:
        writable = False

    return {
        "version": __version__,
        "binaries": await resolver.binary_versions(),
        "paths": {
            "config_dir": str(cfg.config_dir),
            "media_root": str(media_root),
            "media_root_writable": writable,
            "database": str(cfg.db_path),
        },
        "disk": library.disk_usage(media_root),
        "urls": {
            "self_base_url": settings.self_base_url,
            "public_base_url": settings.public_base_url,
            "m3u": f"{settings.self_base_url}/tuner/playlist.m3u?key={settings.row.tuner_token}",
            "xmltv": f"{settings.self_base_url}/tuner/guide.xml?key={settings.row.tuner_token}",
            "eventsub_callback": settings.eventsub_callback_url(),
        },
        "eventsub": await eventsub_service.subscription_health(session),
        "config": {
            "resolver_cache_seconds": cfg.resolver_cache_seconds,
            "max_concurrent_downloads": cfg.max_concurrent_downloads,
            "min_free_disk_gib": cfg.min_free_disk_gib,
        },
    }


@router.get("/events")
async def events(request: Request, _user: AdminUser) -> StreamingResponse:
    """Server-sent events feed used by the UI for live updates."""

    async def stream():
        try:
            async for payload in event_bus.subscribe():
                if await request.is_disconnected():
                    break
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:  # pragma: no cover
            return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
