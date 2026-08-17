"""Twitch EventSub webhook plumbing.

Requirements imposed by Twitch (https://dev.twitch.tv/docs/eventsub/):
  * Webhook transport requires an **app access token**.
  * The callback must be HTTPS on port 443 with a valid certificate.
  * On subscription creation Twitch immediately POSTs a
    `webhook_callback_verification` message; we must echo the `challenge` as the
    raw body (text/plain) within 10 seconds.
  * Every notification carries an HMAC-SHA256 signature over
    `message_id + timestamp + raw_body`, keyed with the transport secret.
  * Messages can be redelivered - dedupe on message id.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.logging_conf import get_logger
from app.models import Channel, EventSubMessage, EventSubSubscription
from app.services.settings_store import ResolvedSettings
from app.services.twitch import EVENTSUB_TYPES, TwitchClient, TwitchError
from app.util import parse_twitch_time, utcnow

log = get_logger(__name__)

HEADER_ID = "twitch-eventsub-message-id"
HEADER_TIMESTAMP = "twitch-eventsub-message-timestamp"
HEADER_SIGNATURE = "twitch-eventsub-message-signature"
HEADER_TYPE = "twitch-eventsub-message-type"
HEADER_SUB_TYPE = "twitch-eventsub-subscription-type"

MESSAGE_TYPE_VERIFICATION = "webhook_callback_verification"
MESSAGE_TYPE_NOTIFICATION = "notification"
MESSAGE_TYPE_REVOCATION = "revocation"

MAX_MESSAGE_AGE = timedelta(minutes=10)
MESSAGE_RETENTION = timedelta(hours=24)


class SignatureError(RuntimeError):
    pass


def verify_signature(
    *, secret: str, message_id: str, timestamp: str, body: bytes, signature: str
) -> bool:
    if not (secret and message_id and timestamp and signature):
        return False
    payload = message_id.encode() + timestamp.encode() + body
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def timestamp_is_fresh(timestamp: str) -> bool:
    parsed = parse_twitch_time(timestamp)
    if parsed is None:
        return False
    return abs(utcnow() - parsed) <= MAX_MESSAGE_AGE


async def remember_message(session: AsyncSession, message_id: str) -> bool:
    """Returns True if this is the first time we have seen `message_id`."""
    if await session.get(EventSubMessage, message_id):
        return False
    session.add(EventSubMessage(message_id=message_id))
    await session.commit()
    return True


async def prune_seen_messages(session: AsyncSession) -> int:
    cutoff = utcnow() - MESSAGE_RETENTION
    # `.execute()` (not `.exec()`) for DML - exec() is for select statements.
    result = await session.execute(
        delete(EventSubMessage).where(EventSubMessage.received_at < cutoff)
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


# ------------------------------------------------------------------- reconciler
@dataclass
class ReconcileReport:
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.skipped_reason is None


async def reconcile_subscriptions(
    session: AsyncSession, settings: ResolvedSettings
) -> ReconcileReport:
    """Make Twitch's subscription list match our enabled channels exactly."""
    report = ReconcileReport()

    if not settings.twitch_configured:
        report.skipped_reason = "Twitch credentials are not configured"
        return report
    if not settings.row.eventsub_enabled:
        report.skipped_reason = "EventSub is disabled (polling mode)"
        return report

    callback = settings.eventsub_callback_url()
    if not callback:
        report.skipped_reason = (
            "EventSub needs a public HTTPS base url; running in polling mode instead"
        )
        return report

    secret = settings.eventsub_secret
    if not secret:
        report.skipped_reason = "EventSub secret is missing"
        return report

    channels = list(
        (
            await session.exec(
                select(Channel).where(Channel.enabled == True)  # noqa: E712
            )
        ).all()
    )
    wanted: dict[tuple[str, str], Channel] = {
        (sub_type, channel.twitch_user_id): channel
        for channel in channels
        for sub_type, _version in EVENTSUB_TYPES
    }

    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        try:
            remote = await twitch.list_eventsub_subscriptions()
        except TwitchError as exc:
            report.errors.append(f"could not list subscriptions: {exc}")
            return report

        # Drop anything pointing at our callback that we no longer want, plus
        # anything Twitch has marked as failed.
        for sub in remote:
            transport = sub.get("transport") or {}
            if transport.get("callback") != callback:
                continue
            sub_type = sub.get("type", "")
            user_id = str((sub.get("condition") or {}).get("broadcaster_user_id") or "")
            key = (sub_type, user_id)
            status = sub.get("status", "")
            healthy = status in {"enabled", "webhook_callback_verification_pending"}
            if key in wanted and healthy:
                report.kept.append(f"{sub_type}:{user_id}")
                wanted.pop(key, None)
                continue
            try:
                await twitch.delete_eventsub_subscription(sub["id"])
                report.deleted.append(f"{sub_type}:{user_id} ({status or 'unwanted'})")
            except TwitchError as exc:
                report.errors.append(f"delete {sub_type}:{user_id}: {exc}")

        versions = dict(EVENTSUB_TYPES)
        for (sub_type, user_id), channel in wanted.items():
            condition: dict[str, str] = {"broadcaster_user_id": user_id}
            try:
                created = await twitch.create_eventsub_subscription(
                    sub_type=sub_type,
                    version=versions[sub_type],
                    condition=condition,
                    callback=callback,
                    secret=secret,
                )
            except TwitchError as exc:
                report.errors.append(f"create {sub_type} for {channel.twitch_login}: {exc}")
                continue
            if created.get("id"):
                session.add(
                    EventSubSubscription(
                        channel_id=channel.id,
                        twitch_sub_id=created["id"],
                        type=sub_type,
                        status=created.get("status", "unknown"),
                    )
                )
                report.created.append(f"{sub_type}:{channel.twitch_login}")

    # Refresh our local mirror of subscription status.
    await session.execute(delete(EventSubSubscription))
    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        try:
            remote = await twitch.list_eventsub_subscriptions()
        except TwitchError:
            remote = []
    by_user = {c.twitch_user_id: c for c in channels}
    for sub in remote:
        transport = sub.get("transport") or {}
        if transport.get("callback") != callback:
            continue
        user_id = str((sub.get("condition") or {}).get("broadcaster_user_id") or "")
        channel = by_user.get(user_id)
        session.add(
            EventSubSubscription(
                channel_id=channel.id if channel else None,
                twitch_sub_id=sub.get("id", ""),
                type=sub.get("type", ""),
                status=sub.get("status", "unknown"),
                last_seen_at=utcnow(),
            )
        )
    await session.commit()

    log.info(
        "eventsub reconciled",
        created=len(report.created),
        deleted=len(report.deleted),
        kept=len(report.kept),
        errors=len(report.errors),
    )
    return report


async def subscription_health(session: AsyncSession) -> dict[str, object]:
    rows = list((await session.exec(select(EventSubSubscription))).all())
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "healthy": all(r.status in {"enabled", "webhook_callback_verification_pending"} for r in rows)
        and bool(rows),
    }


async def teardown_subscriptions(settings: ResolvedSettings) -> int:
    """Delete every subscription pointing at our callback (used on disable)."""
    callback = settings.eventsub_callback_url()
    if not callback or not settings.twitch_configured:
        return 0
    removed = 0
    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        for sub in await twitch.list_eventsub_subscriptions():
            if (sub.get("transport") or {}).get("callback") != callback:
                continue
            try:
                await twitch.delete_eventsub_subscription(sub["id"])
                removed += 1
            except TwitchError as exc:
                log.warning("failed to delete subscription", id=sub.get("id"), error=str(exc))
    return removed
