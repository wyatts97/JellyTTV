"""Web Push subscriptions for the PWA's go-live notifications."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.schemas import (
    ConnectionTest,
    PushSubscribeRequest,
    PushSubscriptionOut,
    PushUnsubscribeRequest,
)
from app.security import AdminUser
from app.services import webpush
from app.services.settings_store import get_settings

router = APIRouter(prefix="/api/push", tags=["push"])


def _to_out(row) -> PushSubscriptionOut:  # noqa: ANN001
    return PushSubscriptionOut(
        id=row.id or 0,
        endpoint=row.endpoint,
        label=row.label,
        failure_count=row.failure_count,
        created_at=row.created_at,
        last_success_at=row.last_success_at,
    )


@router.get("/key")
async def vapid_key(_user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """The public VAPID key a browser subscribes with (`applicationServerKey`)."""
    settings = await get_settings(session)
    return {"public_key": settings.row.vapid_public_key, "enabled": settings.row.webpush_enabled}


@router.get("/subscriptions", response_model=list[PushSubscriptionOut])
async def list_subscriptions(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> list[PushSubscriptionOut]:
    return [_to_out(row) for row in await webpush.list_subscriptions(session)]


@router.post("/subscriptions", response_model=PushSubscriptionOut, status_code=201)
async def subscribe(
    payload: PushSubscribeRequest,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PushSubscriptionOut:
    # The server POSTs to this url on every go-live, so it must at least be a
    # real https push endpoint and not an arbitrary address on the local network.
    parts = urlsplit(payload.endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(status_code=422, detail="push endpoint must be an https url")
    row = await webpush.subscribe(
        session,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        label=payload.label,
    )
    return _to_out(row)


@router.delete("/subscriptions", status_code=204, response_class=Response)
async def unsubscribe(
    payload: PushUnsubscribeRequest,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    # An explicit Response, as in api_channels: under postponed annotations the
    # pinned FastAPI reads `-> None` as a body and refuses the 204 at import.
    await webpush.unsubscribe(session, endpoint=payload.endpoint, sub_id=payload.id)
    return Response(status_code=204)


@router.post("/test", response_model=ConnectionTest)
async def send_test(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> ConnectionTest:
    settings = await get_settings(session)
    report = await webpush.send_all(
        session,
        settings,
        {
            "title": "JellyTTV test",
            "body": "Go-live notifications are working on this device.",
            "icon": "/icon-192.png",
            "tag": "jellyttv-test",
            "url": "/",
        },
    )
    if report.total == 0:
        return ConnectionTest(ok=False, message="No devices are subscribed yet")
    if report.sent == 0:
        return ConnectionTest(
            ok=False,
            message=f"Delivery failed on all {report.total} device(s)",
            details={"removed": report.removed},
        )
    return ConnectionTest(
        ok=True,
        message=f"Sent to {report.sent} of {report.total} device(s)",
        details={"failed": report.failed, "removed": report.removed},
    )
