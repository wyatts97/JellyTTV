"""Job enqueueing helper shared by the API process and the worker."""

from __future__ import annotations

import time
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import get_config
from app.logging_conf import get_logger

log = get_logger(__name__)

_pool: ArqRedis | None = None

# arq refuses an enqueue whose job id is already queued *or* whose result is
# still stored, and `WorkerSettings.keep_result` is an hour. So a fixed job id
# does not mean "coalesce a burst of triggers" - it means "run at most once an
# hour", which is how every reactive Jellyfin guide refresh came to be dropped
# for the rest of the hour after the first one ran. Bucketing the id by a short
# window gives the coalescing that was actually wanted, with no blackout.
COALESCE_SECONDS = 30


def coalesced_job_id(name: str, *, window: int = COALESCE_SECONDS) -> str:
    """A job id that dedupes triggers within `window` seconds and no longer."""
    return f"{name}:{int(time.time() // window)}"


def redis_settings() -> RedisSettings:
    settings = RedisSettings.from_dsn(get_config().redis_url)
    # arq defaults to 5 retries with a 1s pause, which makes every enqueue block
    # for ~11s while Redis is unavailable - long enough to stall API requests.
    # Fail fast instead: enqueue() treats a failure as "not queued" and the
    # worker's cron jobs reconcile the state anyway.
    settings.conn_retries = 1
    settings.conn_timeout = 3
    settings.conn_retry_delay = 1
    return settings


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue(
    task: str,
    *args: Any,
    job_id: str | None = None,
    defer_seconds: int | None = None,
    **kwargs: Any,
) -> str | None:
    """Enqueue a job. `job_id` makes the enqueue idempotent (arq dedupes).

    Note that arq's dedupe covers *stored results* as well as the queue, for
    `keep_result` seconds. A fixed `job_id` therefore suppresses re-runs for far
    longer than most callers intend; use `coalesced_job_id` unless a job really
    should run only once per `keep_result` window.
    """
    try:
        pool = await get_pool()
        job = await pool.enqueue_job(
            task,
            *args,
            _job_id=job_id,
            _defer_by=defer_seconds,
            **kwargs,
        )
    except Exception as exc:  # pragma: no cover - redis down should not 500 the API
        log.error("failed to enqueue job", task=task, error=str(exc))
        return None
    if job is None:
        log.debug("job already queued, skipping duplicate", task=task, job_id=job_id)
        return None
    return job.job_id
