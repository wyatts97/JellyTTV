"""Writes the Jellyfin-facing media tree.

Layout (matches https://jellyfin.org/docs/general/server/media/shows/):

    <media_root>/
      <Channel Display Name>/
        tvshow.nfo
        poster.jpg  fanart.jpg
        Season 2026/
          season.nfo
          <Channel> - S2026E1840 - <Stream Title>.strm | .mp4
          <Channel> - S2026E1840 - <Stream Title>.nfo
          <Channel> - S2026E1840 - <Stream Title>-thumb.jpg

Everything is written atomically (temp file + os.replace) so Jellyfin never sees
a half-written NFO during a scan. Orphaned files we previously created are
removed so a channel's folder always reflects the database.
"""

from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from app.logging_conf import get_logger
from app.models import Channel, Vod, VodMode, VodState
from app.services.episodes import format_episode_tag
from app.util import iso_z, sanitize_filename, utcnow

log = get_logger(__name__)

VIDEO_EXTENSIONS = (".strm", ".mp4", ".mkv", ".ts")
SIDECAR_SUFFIXES = (".nfo", "-thumb.jpg")


@dataclass
class PublishStats:
    series_path: Path
    episodes_written: int = 0
    files_removed: int = 0
    artwork_written: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- io
def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def _xml_to_string(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def _sub(parent: ET.Element, tag: str, value: object | None) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag).text = str(value)


# ------------------------------------------------------------------------ paths
def series_dir_name(channel: Channel) -> str:
    return sanitize_filename(channel.series_dir or channel.display_name, fallback=channel.twitch_login)


def series_path(media_root: Path, channel: Channel) -> Path:
    return media_root / series_dir_name(channel)


def season_path(media_root: Path, channel: Channel, season: int) -> Path:
    return series_path(media_root, channel) / f"Season {season}"


def episode_basename(channel: Channel, vod: Vod) -> str:
    series = sanitize_filename(channel.display_name, max_length=60, fallback=channel.twitch_login)
    tag = format_episode_tag(vod.season, vod.episode)
    title = sanitize_filename(vod.title or "Broadcast", max_length=80, fallback="Broadcast")
    return f"{series} - {tag} - {title}"


def episode_path(media_root: Path, channel: Channel, vod: Vod, extension: str) -> Path:
    return season_path(media_root, channel, vod.season) / f"{episode_basename(channel, vod)}{extension}"


# -------------------------------------------------------------------------- nfo
def build_tvshow_nfo(channel: Channel) -> str:
    root = ET.Element("tvshow")
    _sub(root, "title", channel.display_name)
    _sub(root, "originaltitle", channel.display_name)
    _sub(root, "sorttitle", channel.display_name)
    _sub(root, "plot", channel.description or f"Twitch broadcasts from {channel.display_name}.")
    _sub(root, "studio", "Twitch")
    _sub(root, "genre", "Live Stream")
    _sub(root, "status", "Continuing")
    _sub(root, "lockdata", "true")
    _sub(root, "dateadded", (channel.created_at or utcnow()).strftime("%Y-%m-%d %H:%M:%S"))
    unique = ET.SubElement(root, "uniqueid", {"type": "twitch", "default": "true"})
    unique.text = channel.twitch_user_id
    art = ET.SubElement(root, "art")
    if channel.avatar_url:
        _sub(art, "poster", "poster.jpg")
    if channel.offline_image_url:
        _sub(art, "fanart", "fanart.jpg")
    return _xml_to_string(root)


def build_season_nfo(season: int) -> str:
    root = ET.Element("season")
    _sub(root, "title", f"Season {season}")
    _sub(root, "seasonnumber", season)
    _sub(root, "lockdata", "true")
    return _xml_to_string(root)


def build_episode_nfo(channel: Channel, vod: Vod) -> str:
    root = ET.Element("episodedetails")
    _sub(root, "title", vod.title or "Broadcast")
    _sub(root, "showtitle", channel.display_name)
    _sub(root, "season", vod.season)
    _sub(root, "episode", vod.episode)
    _sub(root, "plot", vod.description or vod.title or "")
    _sub(root, "aired", vod.published_at.date().isoformat())
    _sub(root, "premiered", vod.published_at.date().isoformat())
    _sub(root, "year", vod.published_at.year)
    _sub(root, "studio", "Twitch")
    _sub(root, "genre", "Live Stream")
    _sub(root, "lockdata", "true")
    _sub(root, "dateadded", vod.published_at.strftime("%Y-%m-%d %H:%M:%S"))
    if vod.duration_s:
        # Jellyfin reads <runtime> in minutes. Important for .strm items, which
        # are not probed during a library scan.
        _sub(root, "runtime", max(1, round(vod.duration_s / 60)))
    unique = ET.SubElement(root, "uniqueid", {"type": "twitch", "default": "true"})
    unique.text = vod.twitch_video_id
    if vod.thumbnail_url:
        art = ET.SubElement(root, "art")
        _sub(art, "thumb", f"{episode_basename(channel, vod)}-thumb.jpg")
    return _xml_to_string(root)


# ---------------------------------------------------------------------- artwork
async def _fetch_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        response = await client.get(url, timeout=20.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.debug("artwork fetch failed", url=url, error=str(exc))
        return None
    if response.status_code != 200 or not response.content:
        return None
    if not response.headers.get("content-type", "").startswith("image/"):
        return None
    return response.content


async def _write_image_if_missing(
    client: httpx.AsyncClient, url: str | None, path: Path, *, force: bool = False
) -> bool:
    if not url:
        return False
    if path.exists() and not force:
        return False
    data = await _fetch_image(client, url)
    if data is None:
        return False
    _atomic_write_bytes(path, data)
    return True


# ------------------------------------------------------------------------ strm
def build_strm_content(base_url: str, vod: Vod, token: str | None) -> str:
    url = f"{base_url.rstrip('/')}/vod/{quote(vod.twitch_video_id)}"
    if token:
        url += f"?key={quote(token)}"
    return url + "\n"


# --------------------------------------------------------------------- publish
async def publish_channel(
    channel: Channel,
    vods: list[Vod],
    *,
    media_root: Path,
    self_base_url: str,
    tuner_token: str | None,
    http: httpx.AsyncClient | None = None,
    prune: bool = True,
) -> PublishStats:
    """Materialise `channel` + `vods` onto disk. Idempotent."""
    root = series_path(media_root, channel)
    stats = PublishStats(series_path=root)

    if channel.vod_mode is VodMode.off or not channel.enabled:
        return stats

    root.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(root / "tvshow.nfo", build_tvshow_nfo(channel))

    owns_client = http is None
    client = http or httpx.AsyncClient()
    try:
        if await _write_image_if_missing(client, channel.avatar_url, root / "poster.jpg"):
            stats.artwork_written += 1
        if await _write_image_if_missing(client, channel.offline_image_url, root / "fanart.jpg"):
            stats.artwork_written += 1

        expected: dict[Path, set[str]] = {}
        seasons: set[int] = set()

        for vod in vods:
            if vod.state in (VodState.purged, VodState.skipped):
                continue
            if vod.mode is VodMode.off:
                continue

            season_root = season_path(media_root, channel, vod.season)
            season_root.mkdir(parents=True, exist_ok=True)
            seasons.add(vod.season)
            base = episode_basename(channel, vod)
            expected.setdefault(season_root, set()).add(base)

            media_target: Path | None = None
            if vod.mode is VodMode.archive and vod.state is VodState.complete and vod.file_path:
                # The download job already placed the file; make sure the .strm
                # placeholder is gone so Jellyfin does not show it twice.
                media_target = Path(vod.file_path)
                strm_leftover = season_root / f"{base}.strm"
                if strm_leftover.exists():
                    strm_leftover.unlink(missing_ok=True)
                    stats.files_removed += 1
            elif vod.mode is VodMode.strm or vod.state is not VodState.complete:
                if vod.mode is VodMode.archive and vod.state in (
                    VodState.pending,
                    VodState.queued,
                    VodState.downloading,
                ):
                    # Don't publish a half-downloaded archive episode at all.
                    expected[season_root].discard(base)
                    continue
                media_target = season_root / f"{base}.strm"
                _atomic_write_text(
                    media_target, build_strm_content(self_base_url, vod, tuner_token)
                )

            if media_target is None:
                continue

            _atomic_write_text(season_root / f"{base}.nfo", build_episode_nfo(channel, vod))
            if await _write_image_if_missing(
                client, vod.thumbnail_url, season_root / f"{base}-thumb.jpg"
            ):
                stats.artwork_written += 1
            stats.episodes_written += 1

        for season in seasons:
            season_root = season_path(media_root, channel, season)
            _atomic_write_text(season_root / "season.nfo", build_season_nfo(season))

        if prune:
            stats.files_removed += _prune(root, expected)
    finally:
        if owns_client:
            await client.aclose()

    log.info(
        "published channel to library",
        channel=channel.twitch_login,
        episodes=stats.episodes_written,
        removed=stats.files_removed,
    )
    return stats


def _prune(series_root: Path, expected: dict[Path, set[str]]) -> int:
    """Delete files we own that no longer correspond to a database row."""
    removed = 0
    for season_dir in sorted(series_root.glob("Season *")):
        if not season_dir.is_dir():
            continue
        keep = expected.get(season_dir, set())
        for entry in season_dir.iterdir():
            if not entry.is_file() or entry.name == "season.nfo":
                continue
            base = entry.name
            for suffix in (*VIDEO_EXTENSIONS, *SIDECAR_SUFFIXES):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            else:
                continue
            if base in keep:
                continue
            entry.unlink(missing_ok=True)
            removed += 1
            log.info("pruned orphaned library file", path=str(entry))
        if not any(season_dir.iterdir()):
            season_dir.rmdir()
    return removed


def rename_series(media_root: Path, old_dir: str, new_dir: str) -> Path | None:
    """Move a series folder when the channel's folder name changes.

    Returns the new path if something moved. Renaming must never delete: the
    folder can contain many gigabytes of archived VODs.
    """
    old_name = sanitize_filename(old_dir, fallback="")
    new_name = sanitize_filename(new_dir, fallback="")
    if not old_name or not new_name or old_name == new_name:
        return None

    source = media_root / old_name
    target = media_root / new_name
    if not source.is_dir():
        return None
    if target.exists():
        log.warning(
            "cannot rename series folder, target already exists",
            source=str(source),
            target=str(target),
        )
        return None

    shutil.move(str(source), str(target))
    log.info("renamed series directory", source=str(source), target=str(target))
    return target


def remove_series(media_root: Path, channel: Channel) -> bool:
    """Delete a channel's whole folder (used when a channel is removed)."""
    root = series_path(media_root, channel)
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    log.info("removed series directory", path=str(root))
    return True


def disk_usage(media_root: Path) -> dict[str, int]:
    total, used, free = shutil.disk_usage(media_root)
    library_bytes = 0
    if media_root.exists():
        for path in media_root.rglob("*"):
            if path.is_file():
                try:
                    library_bytes += path.stat().st_size
                except OSError:
                    continue
    return {"total": total, "used": used, "free": free, "library": library_bytes}


def build_publish_report(stats: PublishStats) -> dict[str, object]:
    return {
        "path": str(stats.series_path),
        "episodes": stats.episodes_written,
        "removed": stats.files_removed,
        "artwork": stats.artwork_written,
        "warnings": stats.warnings,
        "at": iso_z(utcnow()),
    }
