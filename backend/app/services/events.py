"""Cross-process event bus used for the UI's live activity feed (SSE).

The API container and the worker container are separate processes, so events are
fanned out through a Redis pub/sub channel. If Redis is unavailable the helpers
degrade to no-ops rather than breaking the request that emitted them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis

from app.config import get_config
from app.logging_conf import get_logger
from app.util import iso_z, utcnow

log = get_logger(__name__)

CHANNEL = "jellyttv:events"

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_config().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish(event_type: str, payload: dict[str, Any] | None = None) -> None:
    message = {
        "type": event_type,
        "at": iso_z(utcnow()),
        "data": payload or {},
    }
    try:
        await get_redis().publish(CHANNEL, json.dumps(message, default=str))
    except Exception as exc:  # pragma: no cover - bus is best-effort
        log.debug("event publish failed", error=str(exc), event=event_type)


async def subscribe() -> AsyncIterator[str]:
    """Yield raw JSON strings for every event published on the bus."""
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
            if message is None:
                yield json.dumps({"type": "ping", "at": iso_z(utcnow()), "data": {}})
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode()
            if data:
                yield data
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
