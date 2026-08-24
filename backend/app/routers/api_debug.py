"""Diagnostics for the HLS proxy.

Answers the question "why does this channel play and that one not" by showing
the raw upstream playlist and the playlist we hand to Jellyfin side by side,
with each segment's ad classification and the sequence number it was given.

Admin-only, never behind the tuner token: the output contains the signed
upstream weaver url.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_db
from app.routers.hls import _channel_quality, _fetch_playlist, _make_resolver
from app.security import AdminUser
from app.services import resolver, stream_session
from app.services.settings_store import get_settings

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/hls/sessions")
async def hls_sessions(_user: AdminUser) -> dict:
    """Every live stream session and its sequence bookkeeping."""
    return {"sessions": stream_session.stats()}


@router.get("/hls/{login}")
async def hls_debug(
    login: str,
    _user: AdminUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    refresh: Annotated[bool, Query(description="Force a fresh streamlink resolve")] = False,
    fmt: Annotated[Literal["json", "text"], Query()] = "json",
):
    settings = await get_settings(session)
    quality = await _channel_quality(session, settings, login)

    if refresh:
        resolver.invalidate_live(login, quality)
        stream_session.drop(login, quality)

    try:
        report = await stream_session.preview(
            login=login,
            quality=quality,
            strip_ads=settings.row.strip_ads,
            resolve=_make_resolver(login, quality, settings),
            fetch=_fetch_playlist,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - diagnostics must report, not crash
        raise HTTPException(status_code=502, detail=f"debug probe failed: {exc}") from exc

    if fmt == "text":
        return PlainTextResponse(_as_text(report))
    return report


def _as_text(report: dict) -> str:
    up = report["upstream"]
    lines = [
        f"channel        : {report['login']} ({report['quality']})",
        f"upstream url   : {report['upstream_url']}",
        f"upstream status: {up['status']}",
        f"upstream mseq  : {up['media_sequence']}  target={up['target_duration']}",
        f"segments       : {up['segment_count']} ({up['ad_segments']} classified as ads)",
        f"low latency    : {up['low_latency']}  dropped={', '.join(up['dropped_tags']) or '-'}",
        f"prefetch       : {len(up['prefetch_uris'])} (never emitted)",
        "",
        "session before this poll:",
        f"  {report['session']}",
        "",
        "result:",
        f"  {report['result']}",
        "",
        "segments:",
    ]
    for seg in report["segments"]:
        flag = f"AD[{seg['ad_source']}]" if seg["is_ad"] else "  --  "
        seen = "seen" if seg["already_emitted"] else "new "
        lines.append(
            f"  #{seg['index']:>3} up={seg['upstream_seq']:<10} {flag:<18} {seen} "
            f"dur={seg['duration']:<6} key={seg['key']}"
        )

    lines += ["", "===== RAW UPSTREAM =====", report["raw"], "", "===== REWRITTEN =====", report["rewritten"]]
    return "\n".join(lines)
