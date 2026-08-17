"""VOD catalogue sync, yt-dlp archiving and retention."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_config
from app.logging_conf import get_logger
from app.models import Channel, Vod, VodMode, VodState
from app.services import episodes as episode_numbering
from app.services import library
from app.services.settings_store import ResolvedSettings
from app.services.twitch import TwitchClient
from app.util import parse_twitch_duration, parse_twitch_time, twitch_thumbnail, utcnow

log = get_logger(__name__)

YTDLP_BIN = "yt-dlp"
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")


class VodError(RuntimeError):
    pass


@dataclass
class SyncResult:
    discovered: int = 0
    created: int = 0
    updated: int = 0
    queued_for_download: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.queued_for_download is None:
            self.queued_for_download = []


# ----------------------------------------------------------------------- queries
async def list_vods(
    session: AsyncSession,
    *,
    channel_id: int | None = None,
    states: list[VodState] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Vod]:
    statement = select(Vod).order_by(Vod.published_at.desc())
    if channel_id is not None:
        statement = statement.where(Vod.channel_id == channel_id)
    if states:
        statement = statement.where(Vod.state.in_(states))  # type: ignore[attr-defined]
    statement = statement.offset(offset).limit(limit)
    return list((await session.exec(statement)).all())


async def channel_vods(session: AsyncSession, channel_id: int) -> list[Vod]:
    return list(
        (
            await session.exec(
                select(Vod)
                .where(Vod.channel_id == channel_id)
                .order_by(Vod.published_at.desc())
            )
        ).all()
    )


async def count_by_state(session: AsyncSession) -> dict[str, int]:
    rows = (await session.exec(select(Vod.state, func.count(Vod.id)).group_by(Vod.state))).all()
    return {str(state.value if hasattr(state, "value") else state): count for state, count in rows}


# -------------------------------------------------------------------------- sync
async def sync_channel_vods(
    session: AsyncSession,
    settings: ResolvedSettings,
    channel: Channel,
    *,
    limit: int = 20,
) -> SyncResult:
    """Pull the channel's recent VOD catalogue from Helix into our database."""
    result = SyncResult()
    if channel.vod_mode is VodMode.off:
        channel.last_vod_sync_at = utcnow()
        session.add(channel)
        await session.commit()
        return result

    if not settings.twitch_configured:
        raise VodError("Twitch credentials are not configured")

    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        videos = await twitch.get_videos(channel.twitch_user_id, video_type="archive", limit=limit)

    result.discovered = len(videos)
    if not videos:
        channel.last_vod_sync_at = utcnow()
        session.add(channel)
        await session.commit()
        return result

    existing = {v.twitch_video_id: v for v in await channel_vods(session, channel.id)}  # type: ignore[arg-type]
    taken = {(v.season, v.episode) for v in existing.values()}

    # Oldest first so episode numbers within a day are assigned chronologically.
    for payload in sorted(videos, key=lambda v: v.get("created_at") or ""):
        video_id = str(payload.get("id"))
        published_at = (
            parse_twitch_time(payload.get("published_at"))
            or parse_twitch_time(payload.get("created_at"))
            or utcnow()
        )
        row = existing.get(video_id)

        if row is None:
            season, episode = episode_numbering.assign_numbers(
                published_at, channel.season_scheme, taken=taken
            )
            taken.add((season, episode))
            row = Vod(
                channel_id=channel.id,  # type: ignore[arg-type]
                twitch_video_id=video_id,
                season=season,
                episode=episode,
                mode=channel.vod_mode,
                state=VodState.pending,
            )
            result.created += 1
        else:
            result.updated += 1

        row.title = payload.get("title") or row.title or "Broadcast"
        row.description = payload.get("description") or row.description
        row.url = payload.get("url") or f"https://www.twitch.tv/videos/{video_id}"
        row.thumbnail_url = twitch_thumbnail(payload.get("thumbnail_url")) or row.thumbnail_url
        row.published_at = published_at
        row.duration_s = parse_twitch_duration(payload.get("duration")) or row.duration_s
        row.updated_at = utcnow()

        # Follow the channel's current mode unless a file is already on disk.
        already_archived = row.state is VodState.complete and bool(row.file_path)
        if not already_archived:
            row.mode = channel.vod_mode
            if channel.vod_mode is VodMode.strm:
                # A .strm link needs no work - it is publishable immediately.
                row.state = VodState.complete
                row.progress = 100.0
            elif channel.vod_mode is VodMode.archive and row.state in (
                VodState.complete,
                VodState.purged,
            ):
                # Switched strm -> archive (or re-enabled after a purge): the
                # file is not on disk, so it needs downloading again.
                row.state = VodState.pending
                row.progress = 0.0
                row.attempts = 0

        if (
            channel.vod_mode is VodMode.archive
            and row.state in (VodState.pending, VodState.failed)
            and row.attempts < 5
        ):
            row.state = VodState.queued
            result.queued_for_download.append(row.id or 0)

        session.add(row)

    channel.last_vod_sync_at = utcnow()
    session.add(channel)
    await session.commit()

    # ids for rows created in this pass are only known after the commit
    if channel.vod_mode is VodMode.archive:
        queued = (
            await session.exec(
                select(Vod.id)
                .where(Vod.channel_id == channel.id)
                .where(Vod.state == VodState.queued)
            )
        ).all()
        result.queued_for_download = [int(i) for i in queued if i]

    log.info(
        "vod sync complete",
        channel=channel.twitch_login,
        discovered=result.discovered,
        created=result.created,
        queued=len(result.queued_for_download),
    )
    return result


# ---------------------------------------------------------------------- download
def _free_gib(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / (1024**3)


async def download_vod(
    session: AsyncSession,
    settings: ResolvedSettings,
    vod: Vod,
    channel: Channel,
    *,
    on_progress: Any = None,
) -> Path:
    """Archive a VOD to disk with yt-dlp. Resumable and retry-friendly."""
    if shutil.which(YTDLP_BIN) is None:
        raise VodError("yt-dlp is not installed in this container")

    cfg = get_config()
    season_dir = library.season_path(cfg.media_root, channel, vod.season)
    season_dir.mkdir(parents=True, exist_ok=True)

    free = _free_gib(season_dir)
    if free < cfg.min_free_disk_gib:
        raise VodError(
            f"only {free:.1f} GiB free, need at least {cfg.min_free_disk_gib:.1f} GiB"
        )

    base = library.episode_basename(channel, vod)
    output_template = str(season_dir / f"{base}.%(ext)s")
    final_path = season_dir / f"{base}.mp4"

    quality = channel.quality or "best"
    fmt = "best" if quality in {"", "best"} else f"best[height<={quality.rstrip('p')}]/best"

    cmd = [
        YTDLP_BIN,
        "--newline",
        "--no-warnings",
        "--no-playlist",
        "--continue",
        "--retries",
        "10",
        "--fragment-retries",
        "20",
        "--concurrent-fragments",
        "4",
        "--merge-output-format",
        "mp4",
        "-f",
        fmt,
        "-o",
        output_template,
        vod.url or f"https://www.twitch.tv/videos/{vod.twitch_video_id}",
    ]
    if settings.twitch_user_token:
        cmd[1:1] = ["--add-header", f"Authorization: OAuth {settings.twitch_user_token}"]

    vod.state = VodState.downloading
    vod.attempts += 1
    vod.error = None
    vod.progress = 0.0
    vod.updated_at = utcnow()
    session.add(vod)
    await session.commit()

    log.info("starting vod download", video_id=vod.twitch_video_id, channel=channel.twitch_login)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    tail: list[str] = []
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]
        match = _PROGRESS_RE.search(line)
        if match:
            percent = float(match.group(1))
            if percent - vod.progress >= 1.0 or percent >= 100.0:
                vod.progress = percent
                session.add(vod)
                await session.commit()
                if on_progress is not None:
                    await on_progress(percent, line)

    await process.wait()

    if process.returncode != 0:
        vod.state = VodState.failed
        vod.error = "\n".join(tail[-6:])[:1000] or f"yt-dlp exited {process.returncode}"
        vod.updated_at = utcnow()
        session.add(vod)
        await session.commit()
        raise VodError(f"yt-dlp failed for {vod.twitch_video_id}: {vod.error}")

    produced = final_path if final_path.exists() else _find_produced(season_dir, base)
    if produced is None:
        vod.state = VodState.failed
        vod.error = "yt-dlp reported success but no output file was found"
        session.add(vod)
        await session.commit()
        raise VodError(vod.error)

    vod.state = VodState.complete
    vod.mode = VodMode.archive
    vod.file_path = str(produced)
    vod.bytes = produced.stat().st_size
    vod.progress = 100.0
    vod.error = None
    vod.updated_at = utcnow()
    session.add(vod)
    await session.commit()

    log.info(
        "vod download complete",
        video_id=vod.twitch_video_id,
        path=str(produced),
        size=vod.bytes,
    )
    return produced


def _find_produced(season_dir: Path, base: str) -> Path | None:
    for extension in (".mp4", ".mkv", ".ts", ".webm"):
        candidate = season_dir / f"{base}{extension}"
        if candidate.exists():
            return candidate
    return None


def delete_vod_file(vod: Vod) -> bool:
    if not vod.file_path:
        return False
    path = Path(vod.file_path)
    removed = False
    if path.exists():
        path.unlink(missing_ok=True)
        removed = True
    for sidecar in (path.with_suffix(".nfo"), path.parent / f"{path.stem}-thumb.jpg"):
        sidecar.unlink(missing_ok=True)
    # Leftover partial downloads
    for partial in path.parent.glob(f"{path.stem}.*.part"):
        partial.unlink(missing_ok=True)
    return removed


# --------------------------------------------------------------------- retention
@dataclass
class RetentionReport:
    channel: str
    deleted: int = 0
    freed_bytes: int = 0
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


async def apply_retention(
    session: AsyncSession, channel: Channel, *, dry_run: bool = False
) -> RetentionReport:
    report = RetentionReport(channel=channel.twitch_login)

    archived = [
        v
        for v in await channel_vods(session, channel.id)  # type: ignore[arg-type]
        if v.state is VodState.complete and v.file_path
    ]
    archived.sort(key=lambda v: v.published_at, reverse=True)

    to_delete: dict[int, str] = {}

    if channel.retention_keep_count is not None and channel.retention_keep_count >= 0:
        for vod in archived[channel.retention_keep_count :]:
            to_delete[vod.id or 0] = f"keep_count>{channel.retention_keep_count}"

    if channel.retention_max_age_days:
        cutoff = utcnow() - timedelta(days=channel.retention_max_age_days)
        for vod in archived:
            if vod.published_at < cutoff:
                to_delete.setdefault(vod.id or 0, f"older_than_{channel.retention_max_age_days}d")

    if channel.retention_max_gb:
        budget = channel.retention_max_gb * (1024**3)
        running = 0
        for vod in archived:
            if vod.id in to_delete:
                continue
            running += vod.bytes or 0
            if running > budget:
                to_delete[vod.id or 0] = f"over_{channel.retention_max_gb}GB"

    by_id = {v.id: v for v in archived}
    for vod_id, reason in to_delete.items():
        vod = by_id.get(vod_id)
        if vod is None:
            continue
        report.reasons.append(f"{vod.twitch_video_id}: {reason}")
        report.freed_bytes += vod.bytes or 0
        report.deleted += 1
        if dry_run:
            continue
        delete_vod_file(vod)
        vod.state = VodState.purged
        vod.file_path = None
        vod.bytes = None
        vod.updated_at = utcnow()
        session.add(vod)

    if not dry_run and report.deleted:
        await session.commit()
        log.info(
            "retention sweep removed archives",
            channel=channel.twitch_login,
            deleted=report.deleted,
            freed=report.freed_bytes,
        )
    return report
