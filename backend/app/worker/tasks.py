"""Background jobs.

Every task is safe to run concurrently with itself being re-enqueued: they take
ids rather than objects, re-read state from the database, and are idempotent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from sqlmodel import select

from app.config import get_config
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Channel, EventLog, Job, JobState, Vod, VodMode, VodState
from app.services import channels as channel_service
from app.services import events as event_bus
from app.services import eventsub as eventsub_service
from app.services import library, vods
from app.services.jellyfin import JellyfinClient, JellyfinError
from app.services.settings_store import get_settings
from app.util import utcnow
from app.worker.queue import enqueue

log = get_logger(__name__)

_download_semaphore: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(get_config().max_concurrent_downloads)
    return _download_semaphore


@asynccontextmanager
async def job_record(
    job_type: str, *, key: str | None = None, payload: dict | None = None
) -> AsyncIterator[Job]:
    async with session_scope() as session:
        job = Job(
            type=job_type,
            key=key,
            payload=payload or {},
            state=JobState.running,
            started_at=utcnow(),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    await event_bus.publish("job.started", {"id": job_id, "type": job_type, "key": key})
    try:
        yield job
    except Exception as exc:
        async with session_scope() as session:
            row = await session.get(Job, job_id)
            if row:
                row.state = JobState.failed
                row.message = str(exc)[:500]
                row.finished_at = utcnow()
                session.add(row)
        await event_bus.publish(
            "job.failed", {"id": job_id, "type": job_type, "key": key, "error": str(exc)[:500]}
        )
        log.error("job failed", job=job_type, key=key, error=str(exc))
        raise
    else:
        async with session_scope() as session:
            row = await session.get(Job, job_id)
            if row:
                row.state = JobState.complete
                row.progress = 100.0
                row.message = job.message
                row.finished_at = utcnow()
                session.add(row)
        await event_bus.publish(
            "job.finished", {"id": job_id, "type": job_type, "key": key, "message": job.message}
        )


async def _log_event(
    category: str, message: str, *, channel_id: int | None = None, level: str = "info"
) -> None:
    async with session_scope() as session:
        session.add(EventLog(category=category, message=message, channel_id=channel_id, level=level))
    await event_bus.publish(
        "log", {"category": category, "message": message, "channel_id": channel_id, "level": level}
    )


# ---------------------------------------------------------------------- eventsub
async def reconcile_eventsub(ctx: dict[str, Any]) -> dict[str, Any]:
    async with job_record("eventsub_reconcile") as job:
        async with session_scope() as session:
            settings = await get_settings(session)
            report = await eventsub_service.reconcile_subscriptions(session, settings)
        job.message = report.skipped_reason or (
            f"created={len(report.created)} deleted={len(report.deleted)} kept={len(report.kept)}"
        )
        if report.errors:
            await _log_event("eventsub", "; ".join(report.errors[:3]), level="error")
        return {
            "created": report.created,
            "deleted": report.deleted,
            "kept": report.kept,
            "errors": report.errors,
            "skipped": report.skipped_reason,
        }


async def prune_eventsub_messages(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        return await eventsub_service.prune_seen_messages(session)


# -------------------------------------------------------------------- live state
async def poll_live(ctx: dict[str, Any]) -> dict[str, bool]:
    """Polling fallback / safety net for go-live detection."""
    async with session_scope() as session:
        settings = await get_settings(session)
        if not settings.twitch_configured:
            return {}
        rows = await channel_service.list_channels(session, enabled_only=True)
        rows = [c for c in rows if c.live_enabled]
        if not rows:
            return {}
        changed = await channel_service.poll_live_state(session, settings, rows)

    for login, is_live in changed.items():
        await event_bus.publish("channel.live" if is_live else "channel.offline", {"login": login})
        await _log_event("live", f"{login} went {'live' if is_live else 'offline'}")
        if not is_live:
            async with session_scope() as session:
                channel = await channel_service.get_channel_by_login(session, login)
            if channel and channel.vod_mode is not VodMode.off:
                # Twitch needs a few minutes to publish the VOD.
                await enqueue(
                    "sync_vods",
                    channel.id,
                    job_id=f"sync_vods:{channel.id}:offline",
                    defer_seconds=900,
                )
    if changed:
        await event_bus.publish("channels.changed", {})
    return changed


async def handle_stream_online(ctx: dict[str, Any], channel_id: int) -> None:
    async with session_scope() as session:
        settings = await get_settings(session)
        channel = await session.get(Channel, channel_id)
        if channel is None:
            return
        await channel_service.poll_live_state(session, settings, [channel])
    await event_bus.publish("channels.changed", {})


async def handle_stream_offline(ctx: dict[str, Any], channel_id: int) -> None:
    async with session_scope() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None:
            return
        await channel_service.mark_offline(session, channel)
        mode = channel.vod_mode
    await event_bus.publish("channels.changed", {})
    if mode is not VodMode.off:
        await enqueue(
            "sync_vods", channel_id, job_id=f"sync_vods:{channel_id}:offline", defer_seconds=900
        )


# -------------------------------------------------------------------------- vods
async def sync_vods(ctx: dict[str, Any], channel_id: int, limit: int = 20) -> dict[str, Any]:
    async with job_record("vod_sync", key=str(channel_id)) as job:
        async with session_scope() as session:
            settings = await get_settings(session)
            channel = await session.get(Channel, channel_id)
            if channel is None:
                job.message = "channel no longer exists"
                return {"skipped": True}
            result = await vods.sync_channel_vods(session, settings, channel, limit=limit)
            login = channel.twitch_login
        job.message = f"discovered={result.discovered} new={result.created}"

    for vod_id in result.queued_for_download:
        await enqueue("download_vod", vod_id, job_id=f"download_vod:{vod_id}")

    await enqueue("publish_channel", channel_id, job_id=f"publish:{channel_id}")
    await event_bus.publish(
        "vods.synced",
        {"channel_id": channel_id, "login": login, "created": result.created},
    )
    return {"discovered": result.discovered, "created": result.created}


async def sync_all_vods(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        rows = await channel_service.list_channels(session, enabled_only=True)
        ids = [c.id for c in rows if c.vod_mode is not VodMode.off and c.id]
    for channel_id in ids:
        await enqueue("sync_vods", channel_id, job_id=f"sync_vods:{channel_id}:periodic")
    return len(ids)


async def download_vod(ctx: dict[str, Any], vod_id: int) -> dict[str, Any]:
    async with _semaphore(), job_record("vod_download", key=str(vod_id)) as job:
        async with session_scope() as session:
            settings = await get_settings(session)
            vod = await session.get(Vod, vod_id)
            if vod is None:
                job.message = "vod no longer exists"
                return {"skipped": True}
            if vod.state is VodState.complete and vod.file_path:
                job.message = "already archived"
                return {"skipped": True}
            channel = await session.get(Channel, vod.channel_id)
            if channel is None or channel.vod_mode is not VodMode.archive:
                vod.state = VodState.skipped
                session.add(vod)
                job.message = "channel is no longer in archive mode"
                return {"skipped": True}

            async def on_progress(percent: float, line: str) -> None:
                await event_bus.publish(
                    "vod.progress",
                    {"vod_id": vod_id, "percent": percent, "line": line[-160:]},
                )

            path = await vods.download_vod(
                session, settings, vod, channel, on_progress=on_progress
            )
            channel_id = channel.id
            title = vod.title
        job.message = str(path)

    await _log_event("vod", f"Archived “{title}”", channel_id=channel_id)
    await enqueue("publish_channel", channel_id, job_id=f"publish:{channel_id}")
    await enqueue("retention_sweep", channel_id, job_id=f"retention:{channel_id}")
    return {"path": str(path)}


async def retention_sweep(ctx: dict[str, Any], channel_id: int | None = None) -> dict[str, Any]:
    async with job_record("retention_sweep", key=str(channel_id or "all")) as job:
        reports: list[dict[str, Any]] = []
        async with session_scope() as session:
            if channel_id is None:
                rows = await channel_service.list_channels(session)
            else:
                channel = await session.get(Channel, channel_id)
                rows = [channel] if channel else []
            for channel in rows:
                report = await vods.apply_retention(session, channel)
                if report.deleted:
                    reports.append(
                        {
                            "channel": report.channel,
                            "deleted": report.deleted,
                            "freed_bytes": report.freed_bytes,
                        }
                    )
        job.message = f"pruned {sum(r['deleted'] for r in reports)} archives"

    for report in reports:
        await enqueue("publish_channel_by_login", report["channel"])
    return {"reports": reports}


# ------------------------------------------------------------------- publishing
async def publish_channel(ctx: dict[str, Any], channel_id: int) -> dict[str, Any]:
    cfg = get_config()
    async with job_record("library_write", key=str(channel_id)) as job:
        async with session_scope() as session:
            settings = await get_settings(session)
            channel = await session.get(Channel, channel_id)
            if channel is None:
                job.message = "channel no longer exists"
                return {"skipped": True}
            rows = await vods.channel_vods(session, channel_id)
            async with httpx.AsyncClient() as client:
                stats = await library.publish_channel(
                    channel,
                    rows,
                    media_root=cfg.media_root,
                    self_base_url=settings.self_base_url,
                    tuner_token=settings.row.tuner_token,
                    http=client,
                )
            auto_refresh = settings.row.jellyfin_auto_refresh and settings.jellyfin_configured
        job.message = f"episodes={stats.episodes_written} removed={stats.files_removed}"

    await event_bus.publish(
        "library.published",
        {"channel_id": channel_id, **library.build_publish_report(stats)},
    )
    if auto_refresh:
        await enqueue("jellyfin_refresh", job_id="jellyfin_refresh", defer_seconds=60)
    return library.build_publish_report(stats)


async def publish_channel_by_login(ctx: dict[str, Any], login: str) -> dict[str, Any]:
    async with session_scope() as session:
        channel = await channel_service.get_channel_by_login(session, login)
        channel_id = channel.id if channel else None
    if channel_id is None:
        return {"skipped": True}
    return await publish_channel(ctx, channel_id)


async def publish_all(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        rows = await channel_service.list_channels(session)
        ids = [c.id for c in rows if c.id]
    for channel_id in ids:
        await enqueue("publish_channel", channel_id, job_id=f"publish:{channel_id}")
    return len(ids)


async def jellyfin_refresh(ctx: dict[str, Any]) -> dict[str, Any]:
    async with job_record("jellyfin_refresh") as job:
        async with session_scope() as session:
            settings = await get_settings(session)
            if not settings.jellyfin_configured:
                job.message = "Jellyfin is not configured"
                return {"skipped": True}
            base = settings.row.jellyfin_url or ""
            key = settings.jellyfin_api_key or ""
            library_id = settings.row.jellyfin_shows_library_id
        try:
            async with JellyfinClient(base, key) as client:
                scope = await client.refresh(library_id=library_id)
        except JellyfinError as exc:
            job.message = str(exc)
            raise
        job.message = f"refreshed ({scope})"
    await event_bus.publish("jellyfin.refreshed", {"scope": scope})
    return {"scope": scope}


# ------------------------------------------------------------------- maintenance
async def refresh_artwork(ctx: dict[str, Any]) -> int:
    async with session_scope() as session:
        settings = await get_settings(session)
        rows = await channel_service.list_channels(session)
        updated = await channel_service.refresh_profiles(session, settings, rows)
    if updated:
        await enqueue("publish_all", job_id="publish_all")
    return updated


async def token_maintenance(ctx: dict[str, Any]) -> bool:
    from app.services.twitch import TwitchClient, TwitchError

    async with session_scope() as session:
        settings = await get_settings(session)
        if not settings.twitch_configured:
            return False
        client_id = settings.twitch_client_id or ""
        client_secret = settings.twitch_client_secret or ""
    try:
        async with TwitchClient(client_id, client_secret) as twitch:
            await twitch.validate_token()
        return True
    except TwitchError as exc:
        log.warning("twitch token validation failed", error=str(exc))
        await _log_event("twitch", f"Token validation failed: {exc}", level="warning")
        return False


async def prune_jobs(ctx: dict[str, Any], keep: int = 500) -> int:
    async with session_scope() as session:
        ids = list(
            (await session.exec(select(Job.id).order_by(Job.created_at.desc()).offset(keep))).all()
        )
        for job_id in ids:
            row = await session.get(Job, job_id)
            if row:
                await session.delete(row)
    return len(ids)
