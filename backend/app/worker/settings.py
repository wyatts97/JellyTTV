"""arq worker entrypoint: `python -m arq app.worker.settings.WorkerSettings`."""

from __future__ import annotations

from typing import Any

from arq import cron

from app.config import get_config
from app.db import dispose_engine, init_db
from app.logging_conf import configure_logging, get_logger
from app.services.events import close_redis
from app.worker import tasks
from app.worker.queue import close_pool
from app.worker.queue import redis_settings as build_redis_settings

log = get_logger(__name__)

FUNCTIONS = [
    tasks.reconcile_eventsub,
    tasks.prune_eventsub_messages,
    tasks.poll_live,
    tasks.handle_stream_online,
    tasks.handle_stream_offline,
    tasks.sync_vods,
    tasks.sync_all_vods,
    tasks.download_vod,
    tasks.retention_sweep,
    tasks.publish_channel,
    tasks.publish_channel_by_login,
    tasks.publish_all,
    tasks.jellyfin_refresh,
    tasks.refresh_artwork,
    tasks.token_maintenance,
    tasks.prune_jobs,
]

CRON_JOBS = [
    # Go-live safety net. Cheap (one Helix call for up to 100 channels) and the
    # only detection mechanism when EventSub is unavailable.
    cron(tasks.poll_live, minute=set(range(0, 60, 2)), run_at_startup=True),
    # Keep Twitch's subscription list in sync with our enabled channels.
    cron(tasks.reconcile_eventsub, minute={7}, run_at_startup=True),
    # Periodic VOD catalogue refresh in case an offline event was missed.
    cron(tasks.sync_all_vods, hour={1, 7, 13, 19}, minute={11}),
    cron(tasks.retention_sweep, hour={3}, minute={17}),
    cron(tasks.refresh_artwork, hour={4}, minute={23}),
    cron(tasks.token_maintenance, minute={31}),
    cron(tasks.prune_eventsub_messages, hour={5}, minute={5}),
    cron(tasks.prune_jobs, hour={5}, minute={35}),
]


async def startup(ctx: dict[str, Any]) -> None:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_format)
    await init_db()
    log.info("worker started", media_root=str(cfg.media_root))


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_pool()
    await close_redis()
    await dispose_engine()
    log.info("worker stopped")


class WorkerSettings:
    functions = FUNCTIONS
    cron_jobs = CRON_JOBS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 8
    job_timeout = 60 * 60 * 8  # long VOD downloads
    keep_result = 3600
    max_tries = 3
    redis_settings = build_redis_settings()
