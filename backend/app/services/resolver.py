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

# How long a single resolver subprocess may run. The default is generous
# because a normal resolve happens once per session and a slow answer still
# beats no stream. Callers on a latency-critical path - the ad-backup search,
# which runs while a client is waiting for a playlist - pass something much
# shorter: there, a slow candidate is worse than no candidate.
DEFAULT_RESOLVE_TIMEOUT = 45.0
BACKUP_RESOLVE_TIMEOUT = 8.0
# Live playback resolves are awaited while a stream session holds its lock, so
# they cannot use the generous default: every poll for that channel queues
# behind them.
LIVE_RESOLVE_TIMEOUT = 15.0

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


# Twitch decides whether to stitch ads in when it mints the playback access
# token, based partly on where the request came from. Asking for the token from
# a region that carries no ad inventory is therefore the one lever that stops
# ads *before* they exist, which is the whole design of TTV LOL PRO: it never
# filters a playlist, it just re-issues the token and manifest requests through
# an HTTP proxy in an ad-free region.
#
# `--stream-url` makes streamlink perform exactly those two requests and then
# print a url and exit, so a single proxy flag covers the same request set the
# extension proxies - and nothing else. Segments and later playlist polls stay
# direct: they carry the video, and pushing that through a volunteer-run proxy
# would be both slower and abusive.
DEFAULT_PROXY_URL = "http://chromium.api.cdn-perfprod.com:2023"


def normalise_proxy_url(value: str | None) -> str | None:
    """Blank -> None (disabled). A bare host:port gets an http:// scheme."""
    proxy = (value or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def configured_proxy(value: str | None) -> str | None:
    """Resolve the stored setting to the proxy actually used.

    `None` means "never configured" - which includes the NULL an upgraded
    database starts with - and selects the default, because the feature ships
    on. An explicit empty string is the off switch. The two are deliberately not
    collapsed: without the distinction there is no way to express "off" that
    survives a restart, since the default would keep reasserting itself.
    """
    if value is None:
        return DEFAULT_PROXY_URL
    return normalise_proxy_url(value)


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


async def _run(cmd: list[str], *, timeout: float = DEFAULT_RESOLVE_TIMEOUT) -> tuple[int, str, str]:
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


def proxy_for(user_token: str | None, proxy_url: str | None) -> str | None:
    """The proxy to actually use, which is never one carrying a credential.

    Routing an authenticated resolve through a third-party proxy would hand that
    OAuth token to whoever runs it, and buys nothing: a Turbo or subscribed
    account already gets an ad-free playlist straight from Twitch, which is the
    outcome the proxy exists to approximate. So the token wins and the proxy is
    dropped - a hard rule, not a warning.
    """
    proxy = normalise_proxy_url(proxy_url)
    if proxy and user_token:
        log.info("not proxying an authenticated resolve; using the token directly")
        return None
    return proxy


def _streamlink_cmd(
    url: str,
    quality: str,
    user_token: str | None,
    player_type: str | None = None,
    proxy_url: str | None = None,
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
        # Undocumented Twitch behaviour, hence configurable rather than
        # hard-coded. Off by default - see the comment on PLAYER_TYPE_NONE.
        cmd += ["--twitch-access-token-param", f"playerType={resolved_player_type}"]
    proxy = proxy_for(user_token, proxy_url)
    if proxy:
        # Covers only the token and manifest requests, because `--stream-url`
        # exits before any media is fetched.
        cmd += ["--http-proxy", proxy]
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
    url: str,
    quality: str,
    user_token: str | None,
    player_type: str | None = None,
    proxy_url: str | None = None,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
) -> str:
    errors: list[str] = []

    if shutil.which(STREAMLINK_BIN):
        # Proxied first, then direct. The proxy is third-party infrastructure
        # maintained for someone else's users: it can vanish, rate-limit, or
        # start refusing non-browser clients without notice, and none of that
        # may be allowed to take a stream down. Ads are worth avoiding; a dead
        # channel is not worth trading for them.
        attempts = [proxy_for(user_token, proxy_url), None]
        if attempts[0] is None:
            attempts = [None]

        for attempt, proxy in enumerate(attempts):
            code, out, err = await _run(
                _streamlink_cmd(url, quality, user_token, player_type, proxy),
                timeout=timeout,
            )
            if code == 0 and out.startswith("http"):
                return out.splitlines()[0].strip()
            combined = f"{err}\n{out}".strip()
            if _looks_offline(combined):
                # Offline is a fact about the channel, not the route - retrying
                # without the proxy would only spawn streamlink for nothing.
                raise ChannelOffline("channel is offline")
            if proxy is not None and attempt + 1 < len(attempts):
                log.warning(
                    "proxied resolve failed; retrying directly",
                    proxy=proxy,
                    error=combined[:200] or f"exit {code}",
                )
                continue
            errors.append(f"streamlink: {combined[:300] or f'exit {code}'}")
    else:
        errors.append("streamlink: binary not found")

    if shutil.which(YTDLP_BIN):
        code, out, err = await _run(_ytdlp_cmd(url, quality), timeout=timeout)
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
    proxy_url: str | None = None,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
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
        resolved = await _resolve(
            url, quality, user_token, player_type, proxy_url, timeout
        )
        _cache[cache_key] = _Entry(url=resolved, expires_at=now + ttl)
        return resolved


def live_cache_key(
    login: str,
    quality: str = "best",
    player_type: str | None = None,
    proxy_url: str | None = None,
) -> str:
    # Player type and proxy are part of the key, not just things we invalidate
    # on: each produces a genuinely different playlist - one carrying ads, one
    # not - so they must never share a cache entry.
    proxy = normalise_proxy_url(proxy_url) or "direct"
    return f"live:{login}:{quality}:{resolve_player_type(player_type)}:{proxy}"


def invalidate_live(
    login: str,
    quality: str = "best",
    player_type: str | None = None,
    proxy_url: str | None = None,
) -> None:
    invalidate(live_cache_key(login, quality, player_type, proxy_url))


async def resolve_live(
    login: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    ttl: float | None = None,
    force: bool = False,
    player_type: str | None = None,
    proxy_url: str | None = None,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
) -> str:
    """Return the upstream media-playlist url for a live channel.

    `ttl` defaults to the long session TTL rather than `resolver_cache_seconds`:
    a stream session pins the url it was given and only asks for a new one when
    upstream actually breaks, so the cache is a warm-start aid, not the thing
    keeping streamlink from being respawned. `force=True` bypasses the cache and
    is used by that failure path.

    `proxy_url` routes the token and manifest requests through an HTTP proxy so
    Twitch mints the token for that proxy's region. Ignored when `user_token` is
    set - see `proxy_for`.
    """
    cfg = get_config()
    return await _resolve_cached(
        live_cache_key(login, quality, player_type, proxy_url),
        f"https://www.twitch.tv/{login}",
        quality,
        user_token,
        float(ttl if ttl is not None else cfg.resolver_session_ttl_seconds),
        force=force,
        player_type=player_type,
        proxy_url=proxy_url,
        timeout=timeout,
    )


async def resolve_vod(
    video_id: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    player_type: str | None = None,
) -> str:
    """Return a playable url for a Twitch VOD. Cached longer than live.

    Deliberately takes no proxy: TTV LOL PRO excludes VODs from proxying too
    (`getFetch.ts` skips numeric ids), because VOD ads are not stitched the same
    way and a proxy would only add latency to a long download. Not accepting the
    argument at all beats accepting one that is silently ignored.
    """
    key = f"vod:{video_id}:{quality}:{resolve_player_type(player_type)}"
    return await _resolve_cached(
        key,
        f"https://www.twitch.tv/videos/{video_id}",
        quality,
        user_token,
        300.0,
        player_type=player_type,
    )
