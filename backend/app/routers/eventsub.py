"""Twitch EventSub webhook receiver.

Deliberately unauthenticated by our own token - Twitch cannot send one. Security
comes from the HMAC signature over the raw body plus timestamp freshness and
message-id replay protection.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.logging_conf import get_logger
from app.models import EventSubSubscription
from app.ratelimit import EVENTSUB_LIMIT, limiter
from app.services import channels as channel_service
from app.services import events as event_bus
from app.services import eventsub as eventsub_service
from app.services.settings_store import get_settings
from app.util import utcnow
from app.worker.queue import enqueue

log = get_logger(__name__)
router = APIRouter(prefix="/eventsub", tags=["eventsub"])


@router.post("/callback")
@limiter.limit(EVENTSUB_LIMIT)
async def callback(
    request: Request,
    # Deliberately `= Depends(...)` rather than the `Annotated[...]` form used
    # everywhere else in this codebase. `@limiter.limit` replaces the endpoint
    # with a wrapper defined inside slowapi, and older FastAPI resolves this
    # module's PEP-563 string annotations against *that* wrapper's globals,
    # where `AsyncSession` and `get_db` do not exist. The annotation then fails
    # to resolve, the parameter stops looking like a dependency, and FastAPI
    # demands it as a required query parameter - so every Twitch delivery got a
    # 422, no subscription ever passed verification, and reconcile churned all
    # of them on a loop. A Depends *default* is read straight off the parameter
    # and never needs the annotation evaluated, so it survives either way.
    session: AsyncSession = Depends(get_db),
) -> Response:
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    message_id = headers.get(eventsub_service.HEADER_ID, "")
    timestamp = headers.get(eventsub_service.HEADER_TIMESTAMP, "")
    signature = headers.get(eventsub_service.HEADER_SIGNATURE, "")
    message_type = headers.get(eventsub_service.HEADER_TYPE, "")

    settings = await get_settings(session)
    secret = settings.eventsub_secret
    if not secret:
        log.error("eventsub callback received but no secret is configured")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not configured")

    if not eventsub_service.verify_signature(
        secret=secret,
        message_id=message_id,
        timestamp=timestamp,
        body=body,
        signature=signature,
    ):
        log.warning("eventsub signature verification failed", message_id=message_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bad signature")

    if not eventsub_service.timestamp_is_fresh(timestamp):
        log.warning("eventsub message too old", message_id=message_id, timestamp=timestamp)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="stale message")

    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    # ------------------------------------------------------- challenge handshake
    if message_type == eventsub_service.MESSAGE_TYPE_VERIFICATION:
        challenge = payload.get("challenge", "")
        log.info("eventsub callback verified", subscription=payload.get("subscription", {}).get("type"))
        return Response(content=challenge, media_type="text/plain")

    if message_type == eventsub_service.MESSAGE_TYPE_REVOCATION:
        subscription = payload.get("subscription", {})
        log.warning(
            "eventsub subscription revoked",
            type=subscription.get("type"),
            status=subscription.get("status"),
        )
        await _record_status(session, subscription)
        await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if message_type != eventsub_service.MESSAGE_TYPE_NOTIFICATION:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------- replay protection
    if not await eventsub_service.remember_message(session, message_id):
        log.debug("ignoring duplicate eventsub delivery", message_id=message_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    subscription = payload.get("subscription", {})
    event = payload.get("event", {}) or {}
    sub_type = subscription.get("type", "")
    broadcaster_id = str(event.get("broadcaster_user_id") or "")

    await _record_status(session, subscription)

    channel = await channel_service.get_channel_by_user_id(session, broadcaster_id)
    if channel is None:
        log.info("eventsub notification for untracked channel", broadcaster_id=broadcaster_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if sub_type == "stream.online":
        log.info("stream.online", login=channel.twitch_login)
        await event_bus.publish("channel.live", {"login": channel.twitch_login})
        await enqueue(
            "handle_stream_online", channel.id, job_id=f"online:{channel.id}:{message_id}"
        )
    elif sub_type == "stream.offline":
        log.info("stream.offline", login=channel.twitch_login)
        await event_bus.publish("channel.offline", {"login": channel.twitch_login})
        await enqueue(
            "handle_stream_offline", channel.id, job_id=f"offline:{channel.id}:{message_id}"
        )
    elif sub_type == "channel.update":
        await channel_service.apply_channel_update(
            session,
            channel,
            title=event.get("title"),
            category=event.get("category_name"),
        )
        await event_bus.publish(
            "channel.updated",
            {
                "login": channel.twitch_login,
                "title": event.get("title"),
                "category": event.get("category_name"),
            },
        )
    else:
        log.debug("unhandled eventsub type", type=sub_type)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _record_status(session: AsyncSession, subscription: dict[str, Any]) -> None:
    sub_id = subscription.get("id")
    if not sub_id:
        return
    from sqlmodel import select

    row = (
        await session.exec(
            select(EventSubSubscription).where(EventSubSubscription.twitch_sub_id == sub_id)
        )
    ).first()
    if row is None:
        row = EventSubSubscription(twitch_sub_id=sub_id, type=subscription.get("type", ""))
    row.status = subscription.get("status", row.status)
    row.last_seen_at = utcnow()
    session.add(row)
    await session.commit()
