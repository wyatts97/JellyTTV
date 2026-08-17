from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.models import Channel, Vod, VodMode, VodState
from app.ratelimit import limiter
from app.schemas import VodOut
from app.security import AdminUser
from app.services import vods as vod_service
from app.util import utcnow
from app.worker.queue import enqueue

router = APIRouter(prefix="/api/vods", tags=["vods"])


async def _login_map(session: AsyncSession) -> dict[int, str]:
    rows = (await session.exec(select(Channel.id, Channel.twitch_login))).all()
    return {int(cid): login for cid, login in rows if cid}


@router.get("", response_model=list[VodOut])
async def list_vods(
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    channel_id: int | None = None,
    state: Annotated[list[VodState] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[VodOut]:
    rows = await vod_service.list_vods(
        session, channel_id=channel_id, states=state, limit=limit, offset=offset
    )
    logins = await _login_map(session)
    return [VodOut.build(row, channel_login=logins.get(row.channel_id)) for row in rows]


@router.get("/summary")
async def summary(
    _user: AdminUser, session: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    counts = await vod_service.count_by_state(session)
    rows = await vod_service.list_vods(session, limit=500)
    return {
        "by_state": counts,
        "total": sum(counts.values()),
        "archived_bytes": sum(r.bytes or 0 for r in rows if r.bytes),
    }


@router.post("/{vod_id}/download")
async def download(
    vod_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    vod = await session.get(Vod, vod_id)
    if vod is None:
        raise HTTPException(status_code=404, detail="VOD not found")
    channel = await session.get(Channel, vod.channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.vod_mode is not VodMode.archive:
        raise HTTPException(
            status_code=409,
            detail=f"{channel.display_name} is not in archive mode - switch it first",
        )

    vod.state = VodState.queued
    vod.error = None
    vod.progress = 0.0
    vod.updated_at = utcnow()
    session.add(vod)
    await session.commit()

    job_id = await enqueue("download_vod", vod_id, job_id=f"download_vod:{vod_id}")
    return {"queued": job_id is not None}


@router.post("/{vod_id}/retry")
async def retry(
    vod_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    vod = await session.get(Vod, vod_id)
    if vod is None:
        raise HTTPException(status_code=404, detail="VOD not found")
    vod.attempts = 0
    vod.error = None
    vod.state = VodState.queued
    session.add(vod)
    await session.commit()
    job_id = await enqueue("download_vod", vod_id, job_id=f"download_vod:{vod_id}:retry")
    return {"queued": job_id is not None}


@router.delete("/{vod_id}/file")
async def delete_file(
    vod_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    vod = await session.get(Vod, vod_id)
    if vod is None:
        raise HTTPException(status_code=404, detail="VOD not found")
    removed = vod_service.delete_vod_file(vod)
    vod.state = VodState.purged
    vod.file_path = None
    vod.bytes = None
    vod.progress = 0.0
    vod.updated_at = utcnow()
    session.add(vod)
    await session.commit()
    await enqueue("publish_channel", vod.channel_id, job_id=f"publish:{vod.channel_id}")
    return {"removed": removed}


@router.post("/{vod_id}/skip")
async def skip(
    vod_id: int,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    vod = await session.get(Vod, vod_id)
    if vod is None:
        raise HTTPException(status_code=404, detail="VOD not found")
    vod.state = VodState.skipped
    vod.updated_at = utcnow()
    session.add(vod)
    await session.commit()
    await enqueue("publish_channel", vod.channel_id, job_id=f"publish:{vod.channel_id}")
    return {"ok": True}


@router.get("/{vod_id}/thumbnail", response_class=Response)
@limiter.limit("60/minute")
async def vod_thumbnail(
    request: Request,
    vod_id: int,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    vod = await session.get(Vod, vod_id)
    if vod is None:
        raise HTTPException(status_code=404, detail="VOD not found")
    if not vod.thumbnail_url:
        raise HTTPException(status_code=404, detail="No thumbnail available")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(vod.thumbnail_url)
            resp.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Failed to fetch thumbnail") from None

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "max-age=300"},
    )
