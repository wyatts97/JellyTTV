from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.crypto import hash_password, verify_password
from app.db import get_db
from app.schemas import LoginRequest, SetupRequest
from app.security import clear_session, issue_session, read_session
from app.services.settings_store import get_settings, get_settings_row, update_settings
from app.util import utcnow

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/session")
async def session_state(
    request: Request, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    row = await get_settings_row(session)
    username = read_session(request)
    return {
        "setup_complete": row.setup_complete,
        "authenticated": bool(username) or not row.setup_complete,
        "username": username or row.admin_username,
    }


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await get_settings_row(session)
    if not row.setup_complete:
        raise HTTPException(status_code=409, detail="Run the setup wizard first")
    if payload.username != (row.admin_username or "") or not verify_password(
        row.admin_password_hash, payload.password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password"
        )
    issue_session(response, payload.username)
    return {"ok": True, "username": payload.username}


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_session(response)
    return {"ok": True}


@router.post("/setup")
async def run_setup(
    payload: SetupRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await get_settings_row(session)
    if row.setup_complete:
        raise HTTPException(status_code=409, detail="Setup has already been completed")

    await update_settings(
        session,
        {
            "twitch_client_id": payload.twitch_client_id.strip(),
            "twitch_client_secret": payload.twitch_client_secret.strip(),
            "jellyfin_url": (payload.jellyfin_url or "").rstrip("/") or None,
            "jellyfin_api_key": payload.jellyfin_api_key or None,
            "jellyfin_shows_library_id": payload.jellyfin_shows_library_id or None,
            "self_base_url": (payload.self_base_url or "").rstrip("/") or None,
            "public_base_url": (payload.public_base_url or "").rstrip("/") or None,
            "eventsub_enabled": payload.eventsub_enabled,
        },
    )

    row = await get_settings_row(session)
    row.admin_username = payload.username.strip()
    row.admin_password_hash = hash_password(payload.password)
    row.setup_complete = True
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()

    issue_session(response, row.admin_username)
    settings = await get_settings(session)
    return {
        "ok": True,
        "username": row.admin_username,
        "eventsub_callback_url": settings.eventsub_callback_url(),
    }


@router.post("/password")
async def change_password(
    payload: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
) -> dict:
    row = await get_settings_row(session)
    if row.setup_complete and not read_session(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    row.admin_username = payload.username.strip() or row.admin_username
    row.admin_password_hash = hash_password(payload.password)
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()
    return {"ok": True}
