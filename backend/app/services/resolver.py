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
from app.services import twitch_playback

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
# stitched in, the only remaining move is to find a clean copy of the same
# stream on another player type, which is what `adblock` does.
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
PLAYER_TYPES = (
    PLAYER_TYPE_NONE,
    "frontpage",
    "thunderdome",
    "embed",
    "autoplay",
    # Twitch's squeezeback preview tier, and the only player type CoolCmd's
    # Alternate Player for Twitch.tv mints its ad-free playlist with. Offered
    # here for the native stream too, but not made the default: preview-tier
    # renditions are exactly the quality trade the comment above warns about.
    # `adblock` reaches for it first when covering a break, where a few seconds
    # of reduced quality is the better deal.
    "picture-by-picture",
)


# The one player type Twitch does not stitch ads into.
#
# Measured live against three channels over seven ad breaks: every player type
# offering the full rendition ladder - `web`, `embed`, `popout`, `mobile_web`,
# `site` - carries the pod at the same moment the native stream does, and so
# does `autoplay`. Only this one stays clean, and it is capped at 360p. That
# trade is the whole basis of `ad_free_source` mode: there is no such thing as
# an ad-free 1080p source to switch to, so the choice is 360p or ads.
AD_FREE_PLAYER_TYPE = "picture-by-picture"


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
# One resolve per key at a time, shared by everyone who asks for it meanwhile.
_inflight: dict[str, asyncio.Task[str]] = {}


def _retrieve(task: asyncio.Task) -> None:
    # A resolve whose every waiter gave up still finishes; read its exception
    # so asyncio does not log it as "never retrieved".
    if not task.cancelled():
        task.exception()


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


def _twitch_args(
    user_token: str | None,
    player_type: str | None,
    device_id: str | None,
) -> list[str]:
    """The Twitch-specific streamlink arguments shared by every invocation."""
    args: list[str] = []
    # This install's device id, on both header spellings Twitch accepts. It used
    # to be the literal `twitch-web-wall-mason`, which every ad-block script
    # publishes and every JellyTTV install therefore shared - a single string
    # Twitch could match on, describing thousands of viewers as one. A stable
    # per-install id is the opposite trade: it says "one returning viewer", and
    # it says the same thing to the ad-event report (services.ad_events), which
    # has to agree with this or the two describe different people.
    if device_id:
        args += [
            "--http-header",
            f"X-Device-Id={device_id}",
            "--http-header",
            f"Device-ID={device_id}",
        ]
    resolved_player_type = resolve_player_type(player_type)
    if resolved_player_type != PLAYER_TYPE_NONE:
        # Undocumented Twitch behaviour, hence configurable rather than
        # hard-coded. Off by default - see the comment on PLAYER_TYPE_NONE.
        args += ["--twitch-access-token-param", f"playerType={resolved_player_type}"]
    if user_token:
        args += ["--twitch-api-header", f"Authorization=OAuth {user_token}"]
    return args


def _streamlink_cmd(
    url: str,
    quality: str,
    user_token: str | None,
    player_type: str | None = None,
    device_id: str | None = None,
) -> list[str]:
    # No `--twitch-low-latency` here: it only changes streamlink's own buffering
    # and prefetch behaviour during playback, and `--stream-url` makes streamlink
    # print a url and exit. It never affected the playlist we were handed.
    return [
        STREAMLINK_BIN,
        "--stream-url",
        # Not `--quiet`: streamlink 8 treats that as "no output at all" and
        # prints nothing, not even the url - which then fell through to yt-dlp.
        "--loglevel",
        "error",
        *_twitch_args(user_token, player_type, device_id),
        url,
        quality or "best",
    ]


def streamlink_stdout_cmd(
    url: str,
    quality: str,
    user_token: str | None,
    player_type: str | None = None,
    device_id: str | None = None,
) -> list[str]:
    """streamlink as the live ingest: the stream itself, as MPEG-TS on stdout.

    This replaces resolving a playlist url and rewriting it ourselves. streamlink
    runs its own HLS client - playlist reloads, segment retries, weaver
    reassignment - and writes one continuous TS byte stream, so there is no
    playlist window, no media sequence and no manifest left for Jellyfin's
    ffmpeg to trip over. Logging goes to stderr, keeping stdout pure TS.
    """
    cfg = get_config()
    return [
        STREAMLINK_BIN,
        "--stdout",
        "--loglevel",
        "info",
        # No `--twitch-disable-ads`: streamlink 8 marks it disabled and slated
        # for removal, and once removed an unknown argument would stop every
        # stream from starting. The ad-free player type carries no ads anyway.
        "--stream-segment-attempts",
        str(cfg.live_segment_attempts),
        "--stream-segment-timeout",
        str(cfg.live_segment_timeout_seconds),
        "--stream-timeout",
        str(cfg.live_stream_timeout_seconds),
        "--hls-live-edge",
        str(cfg.live_hls_live_edge),
        "--hls-playlist-reload-time",
        cfg.live_playlist_reload_time,
        *_twitch_args(user_token, player_type, device_id),
        url,
        quality or "best",
    ]


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


def looks_offline(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _OFFLINE_MARKERS)


async def _resolve_direct(
    login: str,
    quality: str,
    user_token: str | None,
    player_type: str | None,
    device_id: str | None,
    force: bool,
) -> str:
    """Resolve by minting a playback token ourselves. See twitch_playback.

    Milliseconds instead of a streamlink process, which is what makes covering
    an ad break possible at all, and what removes the cold start on first play.
    """
    master = await twitch_playback.master(
        login,
        resolve_player_type(player_type),
        user_token=user_token,
        device_id=device_id,
        force=force,
    )
    variant = twitch_playback.pick_variant(master.variants, quality=quality)
    if variant is None:
        raise twitch_playback.PlaybackError("no playable rendition in the master playlist")
    return variant.url


async def _resolve(
    url: str,
    quality: str,
    user_token: str | None,
    player_type: str | None = None,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    device_id: str | None = None,
    direct_login: str | None = None,
    force: bool = False,
) -> str:
    errors: list[str] = []

    if direct_login:
        try:
            return await _resolve_direct(
                direct_login, quality, user_token, player_type, device_id, force
            )
        except twitch_playback.ChannelOffline as exc:
            raise ChannelOffline(str(exc)) from exc
        except twitch_playback.PlaybackError as exc:
            # Twitch changed something, or the network did. streamlink knows
            # another way in, so this is a fallback rather than a failure.
            log.warning(
                "direct playback resolve failed; falling back to streamlink",
                login=direct_login,
                error=str(exc)[:200],
            )
            errors.append(f"direct: {exc}")

    if shutil.which(STREAMLINK_BIN):
        code, out, err = await _run(
            _streamlink_cmd(url, quality, user_token, player_type, device_id),
            timeout=timeout,
        )
        if code == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        combined = f"{err}\n{out}".strip()
        if looks_offline(combined):
            raise ChannelOffline("channel is offline")
        errors.append(f"streamlink: {combined[:300] or f'exit {code}'}")
    else:
        errors.append("streamlink: binary not found")

    if resolve_player_type(player_type) != DEFAULT_PLAYER_TYPE:
        # yt-dlp cannot ask for a player type, so its answer is always the
        # default, ad-stitched stream. Returning that for the ad-free source or
        # an ad-break backup would silently hand back the very ads the request
        # exists to avoid; failing lets the caller keep what it has.
        errors.append("yt-dlp: skipped, it cannot request a player type")
        raise ResolveError("; ".join(errors))

    if shutil.which(YTDLP_BIN):
        code, out, err = await _run(_ytdlp_cmd(url, quality), timeout=timeout)
        if code == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        combined = f"{err}\n{out}".strip()
        if looks_offline(combined):
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
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    device_id: str | None = None,
    direct_login: str | None = None,
) -> str:
    entry = _cache.get(cache_key)
    now = time.time()
    if entry and entry.expires_at > now and not force:
        return entry.url

    # The resolve runs as its own task and callers await it through a shield.
    # Playlist requests wait on it under a short render deadline, and a streamlink
    # cold start regularly outlasts that deadline: when the waiter's cancellation
    # reached the subprocess, every retry started from scratch and a slow channel
    # could never be resolved at all. Now the work survives the waiter, and the
    # retry joins the resolve already in flight instead of starting another.
    # (A request with `force` joins it too - an in-flight resolve is fresh.)
    task = _inflight.get(cache_key)
    if task is None:

        async def work() -> str:
            try:
                resolved = await _resolve(
                    url,
                    quality,
                    user_token,
                    player_type,
                    timeout,
                    device_id,
                    direct_login=direct_login,
                    force=force,
                )
                _cache[cache_key] = _Entry(url=resolved, expires_at=time.time() + ttl)
                return resolved
            finally:
                _inflight.pop(cache_key, None)

        task = asyncio.create_task(work())
        task.add_done_callback(_retrieve)
        _inflight[cache_key] = task
    return await asyncio.shield(task)


def live_cache_key(
    login: str,
    quality: str = "best",
    player_type: str | None = None,
) -> str:
    # Player type is part of the key, not just something we invalidate on: each
    # produces a genuinely different playlist - one carrying ads, one not - so
    # they must never share a cache entry. The ad-backup search depends on this,
    # because it asks for the same channel and quality on a different type.
    return f"live:{login}:{quality}:{resolve_player_type(player_type)}"


def invalidate_live(
    login: str,
    quality: str = "best",
    player_type: str | None = None,
) -> None:
    invalidate(live_cache_key(login, quality, player_type))


async def resolve_live(
    login: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    ttl: float | None = None,
    force: bool = False,
    player_type: str | None = None,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    device_id: str | None = None,
    direct: bool = True,
) -> str:
    """Return the upstream media-playlist url for a live channel.

    `ttl` defaults to the long session TTL rather than `resolver_cache_seconds`:
    a stream session pins the url it was given and only asks for a new one when
    upstream actually breaks, so the cache is a warm-start aid, not the thing
    keeping streamlink from being respawned. `force=True` bypasses the cache and
    is used by that failure path.

    `direct=False` skips minting the playback token ourselves and goes straight
    to streamlink - the escape hatch for a Twitch API change, and what the
    Jellyfin-facing paths that never needed the speed can keep using.
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
        timeout=timeout,
        device_id=device_id,
        direct_login=login if direct else None,
    )


async def resolve_vod(
    video_id: str,
    *,
    quality: str = "best",
    user_token: str | None = None,
    player_type: str | None = None,
    device_id: str | None = None,
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
        device_id=device_id,
    )
