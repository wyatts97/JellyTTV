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

# Twitch decides whether to stitch ads into the playlist partly from the
# `playerType` sent with the access-token request. Asking for a non-default
# player type is the only thing that stops ads *upstream* - once they are
# stitched in, the proxy can only choose between showing them and cutting a hole
# in the stream.
#
# Not overriding is the default, deliberately. streamlink's own Twitch docs say
# ads still get stitched into the playlist whichever player type you ask for,
# and warn that a non-default one can be denied the highest quality renditions -
# so an override costs resolution and buys no ad reduction. Their maintainer's
# position is blunter still: nothing found so far removes ads for an
# unauthenticated viewer, and the only reliable ad-free playlist comes from a
# Turbo or subscribed account's OAuth token (`twitch_user_token`).
#
# The values stay configurable because this is undocumented Twitch behaviour
# that people do report changing over time - but the default must not trade
# picture quality for a benefit that is not there.
PLAYER_TYPE_NONE = "web"
DEFAULT_PLAYER_TYPE = PLAYER_TYPE_NONE
PLAYER_TYPES = (PLAYER_TYPE_NONE, "frontpage", "thunderdome", "embed", "autoplay")


def resolve_player_type(value: str | None) -> str:
    """Normalise the configured player type (NULL/blank -> the default)."""
    return (value or "").strip() or DEFAULT_PLAYER_TYPE


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


def _streamlink_cmd(
    url: str, quality: str, user_token: str | None, player_type: str | None = None
) -> list[str]:
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
    resolved_player_type = resolve_player_type(player_type)
    if resolved_player_type != PLAYER_TYPE_NONE:
        # The lever that actually prevents stitched ads. Undocumented Twitch
        # behaviour, hence configurable rather than hard-coded.
        cmd += ["--twitch-access-token-param", f"playerType={resolved_player_type}"]
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


async def _resolve(
    url: str, quality: str, user_token: str | None, player_type: str | None = None
) -> str:
    errors: list[str] = []

    if shutil.which(STREAMLINK_BIN):
        code, out, err = await _run(_streamlink_cmd(url, quality, user_token, player_type))
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
    player_type: str | None = None,
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
        resolved = await _resolve(url, quality, user_token, player_type)
        _cache[cache_key] = _Entry(url=resolved, expires_at=now + ttl)
        return resolved


def live_cache_key(login: str, quality: str = "best", player_type: str | None = None) -> str:
    # The player type is part of the key, not just something we invalidate on:
    # two player types resolve to genuinely different playlists (one with ads,
    # one without), so they must never share a cache entry.
    return f"live:{login}:{quality}:{resolve_player_type(player_type)}"


def invalidate_live(login: str, quality: str = "best", player_type: str | None = None) -> None:
    invalidate(live_cache_key(login, quality, player_type))


async def resolve_live(
    login: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    ttl: float | None = None,
    force: bool = False,
    player_type: str | None = None,
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
        live_cache_key(login, quality, player_type),
        f"https://www.twitch.tv/{login}",
        quality,
        user_token,
        float(ttl if ttl is not None else cfg.resolver_session_ttl_seconds),
        force=force,
        player_type=player_type,
    )


async def resolve_vod(
    video_id: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    player_type: str | None = None,
) -> str:
    """Return a playable url for a Twitch VOD. Cached longer than live."""
    key = f"vod:{video_id}:{quality}:{resolve_player_type(player_type)}"
    return await _resolve_cached(
        key,
        f"https://www.twitch.tv/videos/{video_id}",
        quality,
        user_token,
        300.0,
        player_type=player_type,
    )
