"""Endpoints Jellyfin's Live TV tuner consumes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.security import require_tuner_token
from app.services import channels as channel_service
from app.services import tuner
from app.services.settings_store import get_settings

router = APIRouter(prefix="/tuner", tags=["tuner"], dependencies=[Depends(require_tuner_token)])

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@router.get("/playlist.m3u", response_class=Response)
async def playlist(session: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    settings = await get_settings(session)
    rows = await channel_service.list_channels(session, enabled_only=True)
    body = tuner.build_m3u(
        rows,
        base_url=settings.self_base_url,
        token=settings.row.tuner_token,
        include_offline=settings.row.tuner_include_offline,
    )
    return Response(
        content=body,
        media_type="audio/x-mpegurl",
        headers={**NO_CACHE, "Content-Disposition": 'inline; filename="jellyttv.m3u"'},
    )


@router.get("/guide.xml", response_class=Response)
async def guide(session: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    settings = await get_settings(session)
    rows = await channel_service.list_channels(session, enabled_only=True)
    body = tuner.build_xmltv(
        rows,
        window_hours=settings.row.guide_window_hours,
        include_offline=settings.row.tuner_include_offline,
    )
    return Response(
        content=body,
        media_type="application/xml",
        headers={**NO_CACHE, "Content-Disposition": 'inline; filename="jellyttv-guide.xml"'},
    )
