"""Go-live Web Push, delivered by JellyTTV's own PWA.

Each subscribed browser hands us a push-service endpoint plus the keys to
encrypt for it; we sign every request with this install's VAPID key. pywebpush
does the encryption and signing. It is synchronous (requests), so every send
runs on a worker thread.

A push service answers 404 or 410 once a subscription is gone for good - the
user revoked permission, uninstalled the PWA, cleared site data - and that row
is deleted on the spot. Any other failure is counted, and a subscription that
keeps failing is dropped too, so one dead device cannot grow into a pile of
doomed sends on every go-live.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from pywebpush import WebPushException, webpush
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.logging_conf import get_logger
from app.models import PushSubscription
from app.services.settings_store import ResolvedSettings
from app.util import utcnow

log = get_logger(__name__)

# Long enough to reach a phone that was briefly offline, short enough that a
# stale "is live" never arrives after the stream is probably over.
TTL_SECONDS = 600
SEND_TIMEOUT_SECONDS = 10.0
MAX_FAILURES = 5
GONE_STATUSES = {404, 410}


@dataclass(slots=True)
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    removed: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.failed


def _send_one(sub: PushSubscription, data: str, private_key: str, subject: str) -> int:
    """Blocking send. Returns the push service's status (0 on transport error)."""
    try:
        response = webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=data,
            vapid_private_key=private_key,
            # A fresh dict per call: pywebpush writes `aud` and `exp` into it.
            vapid_claims={"sub": subject},
            ttl=TTL_SECONDS,
            timeout=SEND_TIMEOUT_SECONDS,
            headers={"Urgency": "high"},
        )
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 0
    except Exception as exc:  # noqa: BLE001 - one bad device must not stop the rest
        log.debug("web push transport error", endpoint=sub.endpoint[:60], error=str(exc))
        return 0
    return getattr(response, "status_code", 201)


async def send_all(
    session: AsyncSession, settings: ResolvedSettings, payload: dict
) -> DeliveryReport:
    """Push `payload` to every subscribed device, pruning dead subscriptions."""
    report = DeliveryReport()
    private_key = settings.vapid_private_key
    if not private_key:
        return report
    subs = list((await session.exec(select(PushSubscription))).all())
    if not subs:
        return report

    data = json.dumps(payload)
    subject = settings.vapid_subject
    statuses = await asyncio.gather(
        *(asyncio.to_thread(_send_one, sub, data, private_key, subject) for sub in subs)
    )

    now = utcnow()
    for sub, code in zip(subs, statuses, strict=True):
        if 200 <= code < 300:
            report.sent += 1
            sub.failure_count = 0
            sub.last_success_at = now
            session.add(sub)
        elif code in GONE_STATUSES:
            report.removed += 1
            report.failed += 1
            await session.delete(sub)
        else:
            report.failed += 1
            sub.failure_count += 1
            if sub.failure_count >= MAX_FAILURES:
                report.removed += 1
                await session.delete(sub)
            else:
                session.add(sub)
            log.warning(
                "web push delivery failed",
                endpoint=sub.endpoint[:60],
                status=code,
                failures=sub.failure_count,
            )
    await session.commit()
    return report


async def subscribe(
    session: AsyncSession, *, endpoint: str, p256dh: str, auth: str, label: str | None
) -> PushSubscription:
    """Upsert by endpoint: re-subscribing the same browser refreshes its keys."""
    row = (
        await session.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    ).first()
    if row is None:
        row = PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth, label=label)
    else:
        row.p256dh = p256dh
        row.auth = auth
        row.failure_count = 0
        if label:
            row.label = label
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def unsubscribe(
    session: AsyncSession, *, endpoint: str | None = None, sub_id: int | None = None
) -> bool:
    query = select(PushSubscription)
    if endpoint is not None:
        query = query.where(PushSubscription.endpoint == endpoint)
    elif sub_id is not None:
        query = query.where(PushSubscription.id == sub_id)
    else:
        return False
    row = (await session.exec(query)).first()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def list_subscriptions(session: AsyncSession) -> list[PushSubscription]:
    return list(
        (await session.exec(select(PushSubscription).order_by(PushSubscription.created_at))).all()
    )
