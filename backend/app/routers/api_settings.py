from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app.crypto import random_token
from app.db import get_db
from app.logging_conf import get_logger
from app.schemas import ConnectionTest, JellyfinLibraryOut, SettingsOut, SettingsUpdate, settings_out
from app.security import AdminUser
from app.services import eventsub as eventsub_service
from app.services.jellyfin import JellyfinClient, JellyfinError
from app.services.settings_store import get_settings, get_settings_row, update_settings
from app.services.twitch import TwitchClient, TwitchError
from app.worker.queue import enqueue

log = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=SettingsOut)
async def read_settings(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> SettingsOut:
    settings = await get_settings(session)
    return settings_out(settings.row, resolved=settings)


@router.put("/settings", response_model=SettingsOut)
async def write_settings(
    payload: SettingsUpdate,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SettingsOut:
    before = await get_settings(session)
    was_eventsub = before.row.eventsub_enabled
    previous_callback = before.eventsub_callback_url()

    values = payload.model_dump(exclude_unset=True)
    # `eventsub_enabled` and empty-string clears must survive the None filter in
    # update_settings, so handle booleans explicitly.
    row = await update_settings(session, {k: v for k, v in values.items() if v is not None})
    if "eventsub_enabled" in values and values["eventsub_enabled"] is not None:
        row.eventsub_enabled = bool(values["eventsub_enabled"])
        session.add(row)
        await session.commit()

    settings = await get_settings(session)

    if settings.row.eventsub_enabled != was_eventsub or (
        settings.eventsub_callback_url() != previous_callback
    ):
        if settings.row.eventsub_enabled:
            await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")
        else:
            try:
                removed = await eventsub_service.teardown_subscriptions(before)
                log.info("removed eventsub subscriptions after disable", count=removed)
            except TwitchError as exc:
                log.warning("could not tear down subscriptions", error=str(exc))

    # Base url or token changes invalidate every .strm file we wrote.
    if {"self_base_url", "public_base_url"} & values.keys():
        await enqueue("publish_all", job_id="publish_all")

    return settings_out(settings.row, resolved=settings)


@router.post("/settings/rotate-tuner-token", response_model=SettingsOut)
async def rotate_tuner_token(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> SettingsOut:
    row = await get_settings_row(session)
    row.tuner_token = random_token()
    session.add(row)
    await session.commit()
    # Every .strm file embeds the token, so they all need rewriting.
    await enqueue("publish_all", job_id="publish_all")
    settings = await get_settings(session)
    return settings_out(settings.row, resolved=settings)


@router.post("/twitch/test", response_model=ConnectionTest)
async def test_twitch(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    client_id: str | None = None,
    client_secret: str | None = None,
) -> ConnectionTest:
    settings = await get_settings(session)
    cid = client_id or settings.twitch_client_id
    secret = client_secret or settings.twitch_client_secret
    if not cid or not secret:
        return ConnectionTest(ok=False, message="Client id and secret are required")
    try:
        async with TwitchClient(cid, secret) as twitch:
            info = await twitch.validate_token()
    except TwitchError as exc:
        return ConnectionTest(ok=False, message=str(exc), details={"body": exc.body or ""})
    return ConnectionTest(
        ok=True,
        message="Twitch credentials are valid",
        details={"client_id": info.get("client_id"), "expires_in": info.get("expires_in")},
    )


@router.post("/jellyfin/test", response_model=ConnectionTest)
async def test_jellyfin(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    url: str | None = None,
    api_key: str | None = None,
) -> ConnectionTest:
    settings = await get_settings(session)
    base = (url or settings.row.jellyfin_url or "").rstrip("/")
    key = api_key or settings.jellyfin_api_key
    if not base or not key:
        return ConnectionTest(ok=False, message="Jellyfin url and API key are required")
    try:
        async with JellyfinClient(base, key) as client:
            info = await client.system_info()
    except JellyfinError as exc:
        return ConnectionTest(ok=False, message=str(exc))
    return ConnectionTest(
        ok=True,
        message=f"Connected to {info.get('ServerName', 'Jellyfin')}",
        details={"version": info.get("Version"), "id": info.get("Id")},
    )


@router.get("/jellyfin/libraries", response_model=list[JellyfinLibraryOut])
async def jellyfin_libraries(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    url: str | None = None,
    api_key: str | None = None,
) -> list[JellyfinLibraryOut]:
    settings = await get_settings(session)
    base = (url or settings.row.jellyfin_url or "").rstrip("/")
    key = api_key or settings.jellyfin_api_key
    if not base or not key:
        raise HTTPException(status_code=400, detail="Jellyfin url and API key are required")
    try:
        async with JellyfinClient(base, key) as client:
            libraries = await client.libraries()
    except JellyfinError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        JellyfinLibraryOut(
            id=lib.id, name=lib.name, collection_type=lib.collection_type, locations=lib.locations
        )
        for lib in libraries
    ]


@router.post("/jellyfin/refresh")
async def trigger_jellyfin_refresh(_user: AdminUser) -> dict:
    job_id = await enqueue("jellyfin_refresh", job_id="jellyfin_refresh")
    return {"queued": job_id is not None}


@router.post("/eventsub/reconcile")
async def reconcile_eventsub(_user: AdminUser) -> dict:
    job_id = await enqueue("reconcile_eventsub", job_id="reconcile_eventsub")
    return {"queued": job_id is not None}


@router.get("/eventsub/status")
async def eventsub_status(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    settings = await get_settings(session)
    health = await eventsub_service.subscription_health(session)
    return {
        "enabled": settings.row.eventsub_enabled,
        "possible": settings.eventsub_possible,
        "callback_url": settings.eventsub_callback_url(),
        "mode": "webhook" if settings.row.eventsub_enabled and settings.eventsub_possible else "polling",
        **health,
    }
