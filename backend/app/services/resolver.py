"""Resolve a Twitch channel/VOD into a playable HLS url.

streamlink is the primary resolver (it understands Twitch's access-token dance
and low-latency playlists); yt-dlp is the fallback. Results are cached briefly
so that Jellyfin re-requesting the playlist every few seconds does not spawn a
subprocess each time.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass

from app.config import get_config
from app.logging_conf import get_logger

log = get_logger(__name__)

STREAMLINK_BIN = "streamlink"
YTDLP_BIN = "yt-dlp"


class ResolveError(RuntimeError):
    pass


class ChannelOffline(ResolveError):
    pass


@dataclass(slots=True)
class _Entry:
    url: str
    expires_at: float


_cache: dict[str, _Entry] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def invalidate(key: str | None = None) -> None:
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


async def _run(cmd: list[str], *, timeout: float = 45.0) -> tuple[int, str, str]:
    log.debug("running resolver command", cmd=" ".join(cmd[:3]) + " ...")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ResolveError(f"{cmd[0]} timed out after {timeout}s") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace").strip(),
        stderr.decode("utf-8", "replace").strip(),
    )


def binaries_available() -> dict[str, bool]:
    return {
        "streamlink": shutil.which(STREAMLINK_BIN) is not None,
        "yt-dlp": shutil.which(YTDLP_BIN) is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


async def binary_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name, args in (
        ("streamlink", [STREAMLINK_BIN, "--version"]),
        ("yt-dlp", [YTDLP_BIN, "--version"]),
        ("ffmpeg", ["ffmpeg", "-version"]),
    ):
        if shutil.which(args[0]) is None:
            versions[name] = None
            continue
        try:
            code, out, _err = await _run(args, timeout=15)
            versions[name] = out.splitlines()[0].strip() if code == 0 and out else None
        except ResolveError:
            versions[name] = None
    return versions


def _streamlink_cmd(url: str, quality: str, user_token: str | None) -> list[str]:
    # No `--twitch-low-latency` here: it only changes streamlink's own buffering
    # and prefetch behaviour during playback, and `--stream-url` makes streamlink
    # print a url and exit. It never affected the playlist we were handed.
    cmd = [
        STREAMLINK_BIN,
        "--stream-url",
        "--quiet",
        # Ad-solution headers that make Twitch serve fewer stitched ads.
        "--http-header",
        "X-Device-Id=twitch-web-wall-mason",
        "--http-header",
        "Device-ID=twitch-web-wall-mason",
    ]
    if user_token:
        cmd += ["--twitch-api-header", f"Authorization=OAuth {user_token}"]
    cmd += [url, quality or "best"]
    return cmd


def _ytdlp_cmd(url: str, quality: str) -> list[str]:
    fmt = "best" if quality in {"", "best"} else f"best[height<={quality.rstrip('p')}]/best"
    return [YTDLP_BIN, "-g", "--no-warnings", "--no-playlist", "-f", fmt, url]


_OFFLINE_MARKERS = (
    "no playable streams found",
    "is offline",
    "not currently live",
    "userNotLive",
    "this channel is offline",
)


def _looks_offline(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _OFFLINE_MARKERS)


async def _resolve(url: str, quality: str, user_token: str | None) -> str:
    errors: list[str] = []

    if shutil.which(STREAMLINK_BIN):
        code, out, err = await _run(_streamlink_cmd(url, quality, user_token))
        if code == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        combined = f"{err}\n{out}".strip()
        if _looks_offline(combined):
            raise ChannelOffline("channel is offline")
        errors.append(f"streamlink: {combined[:300] or f'exit {code}'}")
    else:
        errors.append("streamlink: binary not found")

    if shutil.which(YTDLP_BIN):
        code, out, err = await _run(_ytdlp_cmd(url, quality))
        if code == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        combined = f"{err}\n{out}".strip()
        if _looks_offline(combined):
            raise ChannelOffline("channel is offline")
        errors.append(f"yt-dlp: {combined[:300] or f'exit {code}'}")
    else:
        errors.append("yt-dlp: binary not found")

    raise ResolveError("; ".join(errors))


async def _resolve_cached(
    cache_key: str,
    url: str,
    quality: str,
    user_token: str | None,
    ttl: float,
    *,
    force: bool = False,
) -> str:
    entry = _cache.get(cache_key)
    now = time.time()
    if entry and entry.expires_at > now and not force:
        return entry.url

    async with _lock_for(cache_key):
        entry = _cache.get(cache_key)
        now = time.time()
        if entry and entry.expires_at > now and not force:
            return entry.url
        resolved = await _resolve(url, quality, user_token)
        _cache[cache_key] = _Entry(url=resolved, expires_at=now + ttl)
        return resolved


def live_cache_key(login: str, quality: str = "best") -> str:
    return f"live:{login}:{quality}"


def invalidate_live(login: str, quality: str = "best") -> None:
    invalidate(live_cache_key(login, quality))


async def resolve_live(
    login: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    ttl: float | None = None,
    force: bool = False,
) -> str:
    """Return the upstream media-playlist url for a live channel.

    `ttl` defaults to the long session TTL rather than `resolver_cache_seconds`:
    a stream session pins the url it was given and only asks for a new one when
    upstream actually breaks, so the cache is a warm-start aid, not the thing
    keeping streamlink from being respawned. `force=True` bypasses the cache and
    is used by that failure path.
    """
    cfg = get_config()
    return await _resolve_cached(
        live_cache_key(login, quality),
        f"https://www.twitch.tv/{login}",
        quality,
        user_token,
        float(ttl if ttl is not None else cfg.resolver_session_ttl_seconds),
        force=force,
    )


async def resolve_vod(
    video_id: str, *, quality: str = "best", user_token: str | None = None
) -> str:
    """Return a playable url for a Twitch VOD. Cached longer than live."""
    key = f"vod:{video_id}:{quality}"
    return await _resolve_cached(
        key,
        f"https://www.twitch.tv/videos/{video_id}",
        quality,
        user_token,
        300.0,
    )
