"""Backup-stream ad avoidance.

Technique adapted from TTV-AB by GosuDRM - https://github.com/GosuDRM/TTV-AB
(MIT-based licence with attribution) and from the VAFT / `twitch-videoad`
userscripts. No source was copied; the approach and its constants are
reimplemented here.

The insight this module exists for: Twitch stitches ads **per token**, not per
channel. A playback token minted for a different `playerType` on the same
channel usually comes back *clean*, carrying the same live content at the same
moment. So an ad break does not have to mean dead air - there is another copy of
the stream to switch to, and the break becomes a seam instead of a hole.

Two things decide whether that seam is invisible, and both were wrong here
before:

* **Speed.** Candidates used to be resolved by spawning streamlink - up to 8s
  each, one per playlist poll - so a break ran on black hold segments for
  seconds before anything covered it. Tokens are now minted directly
  (`services.twitch_playback`), which costs milliseconds, so the whole rotation
  runs concurrently inside a single poll.
* **Resolution.** The old rotation led with a `picture-by-picture` 360p bridge
  and upgraded back afterwards, which is two resolution changes per break, each
  a buffer re-initialisation in the player. `embed` and `popout` carry the full
  rendition ladder, so the break is covered at *the same picture* and nothing
  about the media changes. Lower renditions are still accepted rather than
  showing black - they are just no longer the first thing tried.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.logging_conf import get_logger
from app.services import hls, twitch_playback

log = get_logger(__name__)

# The player types worth asking for, best first.
#
# `embed` and `popout` mint the whole ladder, so they can cover a break without
# changing the picture. `autoplay` (android) and `picture-by-picture` are
# preview tiers capped at 360p: worth having, because a clean 360p beats a black
# screen, but only after the full-quality types have been tried.
BACKUP_PLAYER_TYPES = (
    "embed",
    "popout",
    "autoplay",
    "picture-by-picture",
)

# Types that only ever offer preview-tier renditions. Kept apart so a rotation
# can try everything that might match the current picture before settling.
LOW_QUALITY_PLAYER_TYPES = frozenset({"autoplay", "picture-by-picture"})

# `picture-by-picture` remains the fallback of last resort, at its only
# meaningful rendition. Named for the session, which still calls a degraded
# backup a "bridge" and probes for full quality behind it.
FAST_BRIDGE_TYPE = "picture-by-picture"
FAST_BRIDGE_QUALITY = "360p"
# How long a degraded bridge is held before looking for full quality behind it.
BRIDGE_HOLD_SECONDS = 8.0
# How many times one break may rotate off a bridge in search of full quality.
MAX_BRIDGE_UPGRADES = 2

# The whole rotation now runs concurrently, so it is bounded by one deadline
# rather than by attempts. Comfortably inside the router's
# PLAYLIST_DEADLINE_SECONDS, and far inside a poll interval.
SEARCH_DEADLINE_SECONDS = 2.5

# How long a player type stays out of the rotation after failing, by reason.
# An ad-marked type is likely to stay ad-marked for the length of the pod, so it
# waits longest; a transport error is probably transient. Shorter than they were
# when a retry cost a streamlink spawn.
COOLDOWNS = {
    "ad-marked": 10.0,
    "stalled": 6.0,
    "not-playable": 2.0,
    "error": 1.5,
}

# How long to stop searching entirely after a full rotation found nothing clean.
# Was 30s when an attempt cost seconds; a whole rotation now costs a fraction of
# one, and a break where every type is dirty can change on the next pod.
EXHAUSTED_COOLDOWN = 5.0

# How long a candidate's "clean" verdict is worth anything.
#
# A candidate is accepted on the strength of one playlist fetched at one moment.
# That playlist covers a few seconds of a live stream, so the verdict expires
# about as fast: the same player type can be clean when probed and stitched
# moments later - which is exactly what happens when a search is started early,
# before the break has filled the window.
CANDIDATE_STALE_SECONDS = 8.0

# Consecutive clean polls of the native stream before switching back. Matches
# TTV-AB's AD_END_MIN_CLEAN_PLAYLISTS: one clean poll is routinely a gap between
# two pods rather than the end of the break.
MIN_CLEAN_POLLS_TO_RESUME = 3


@dataclass
class BackupCandidate:
    """A player type that came back clean, and the playlist proving it."""

    player_type: str
    quality: str
    url: str
    playlist: str
    # True when this was accepted at a different picture than the session is
    # serving. The session holds it as a bridge and probes for full quality.
    is_bridge: bool = False
    # When the playlist backing this verdict was fetched (`time.monotonic`).
    found_at: float = field(default_factory=time.monotonic)

    def is_stale(self, now: float) -> bool:
        """Has this candidate's clean verdict expired? See CANDIDATE_STALE_SECONDS."""
        return now - self.found_at > CANDIDATE_STALE_SECONDS


@dataclass
class BackupState:
    """Per-session bookkeeping for the backup search."""

    active: BackupCandidate | None = None
    cooldowns: dict[str, float] = field(default_factory=dict)
    searching: bool = False
    searches: int = 0

    # Player types caught carrying the ad during the break in progress. Twitch
    # does not un-insert a pod, so a type that was stitched once stays stitched
    # for the rest of it: re-promoting it a few seconds later buys a seam and a
    # run of black, which is what made a single midroll look like the player was
    # shuffling between streams. Cleared when the break ends.
    stitched_this_break: set[str] = field(default_factory=set)
    # Set when a whole rotation came back with nothing clean; no new search
    # starts before this.
    exhausted_until: float = 0.0
    # Cost of the last rotation, surfaced in the debug snapshot.
    last_attempt_seconds: float = 0.0

    def available_types(self, exclude: str | None, now: float) -> list[str]:
        """Player types worth trying, best first, minus the native one."""
        return [
            pt
            for pt in BACKUP_PLAYER_TYPES
            if pt != exclude
            and pt not in self.stitched_this_break
            and self.cooldowns.get(pt, 0.0) <= now
        ]

    def penalise(self, player_type: str, reason: str, now: float) -> None:
        self.cooldowns[player_type] = now + COOLDOWNS.get(reason, COOLDOWNS["error"])
        log.debug(
            "backup player type cooling down",
            player_type=player_type,
            reason=reason,
        )

    def clear(self) -> None:
        self.active = None


def is_playable(playlist: str) -> bool:
    """Does this playlist actually carry media?

    TTV-AB's `_playlistHasMediaSegments`. A token can resolve and a playlist can
    parse while containing nothing to play - switching to that would trade an ad
    for a stall.
    """
    return "#EXTINF" in playlist or "#EXT-X-PART:" in playlist


def is_clean(playlist: str) -> bool:
    """Is this playlist free of ad markers *everywhere*?

    Deliberately stricter than the per-segment marking used on the native
    stream. A backup only helps if the whole thing is clean: switching to a
    playlist that is itself mid-pod just moves the problem.
    """
    return not hls.has_ad_markers(playlist)


def accepts(playlist: str) -> tuple[bool, str]:
    """TTV-AB's promotion policy - playable first, then clean."""
    if not is_playable(playlist):
        return False, "not-playable"
    if not is_clean(playlist):
        return False, "ad-marked"
    return True, "clean-playable"


@dataclass(slots=True)
class _Probe:
    """One player type's answer, before anything is chosen."""

    player_type: str
    candidate: BackupCandidate | None = None
    exact: bool = False
    pixels: int = 0
    reason: str | None = None


async def _probe_player_type(
    *,
    login: str,
    player_type: str,
    quality: str,
    match: twitch_playback.Variant | None,
    fetch,
    user_token: str | None,
    device_id: str | None,
    force: bool,
    exact_only: bool,
) -> _Probe:
    """Mint a token for one player type and judge the playlist it leads to."""
    probe = _Probe(player_type=player_type)
    try:
        master = await twitch_playback.master(
            login,
            player_type,
            user_token=user_token,
            device_id=device_id,
            force=force,
        )
    except twitch_playback.ChannelOffline:
        probe.reason = "offline"
        return probe
    except twitch_playback.PlaybackError as exc:
        probe.reason = "error"
        log.debug(
            "backup token failed", login=login, player_type=player_type, error=str(exc)[:160]
        )
        return probe

    variant = twitch_playback.pick_variant(master.variants, quality=quality, match=match)
    if variant is None:
        probe.reason = "not-playable"
        return probe

    exact = bool(match is not None and variant.matches(match))
    if exact_only and not exact:
        # The upgrade probe behind a bridge: another degraded rendition would
        # buy a second seam for no picture.
        probe.reason = "degraded"
        return probe

    status, playlist = await fetch(variant.url)
    if status != 200 or not playlist:
        # A dead variant url means the cached master is stale, not that this
        # player type is bad.
        twitch_playback.invalidate(login, player_type)
        probe.reason = "error"
        return probe

    ok, reason = accepts(playlist)
    if not ok:
        probe.reason = reason
        return probe

    probe.candidate = BackupCandidate(
        player_type=player_type,
        quality=variant.quality,
        url=variant.url,
        playlist=playlist,
        is_bridge=match is not None and not exact,
    )
    probe.exact = exact
    probe.pixels = variant.pixels
    return probe


async def find_backup(
    *,
    login: str,
    quality: str,
    native_player_type: str | None,
    state: BackupState,
    fetch,
    user_token: str | None = None,
    device_id: str | None = None,
    full_quality_only: bool = False,
    native_url: str | None = None,
) -> BackupCandidate | None:
    """Try every available player type at once and return the best clean one.

    This used to be one candidate per poll, because each one cost a streamlink
    spawn and walking the rotation inside a single request could block a
    playlist for a minute. Minting tokens directly costs milliseconds, so the
    whole rotation now runs concurrently under one deadline and a break is
    covered on the first poll that notices it.

    "Best" means: the same picture as the native stream wherever possible, then
    the largest clean picture available, then nothing - in which case the caller
    holds and asks again.
    """
    now = time.monotonic()
    if now < state.exhausted_until:
        return None

    types = state.available_types(native_player_type, now)
    if not types:
        state.exhausted_until = now + EXHAUSTED_COOLDOWN
        log.debug("no backup player types available", login=login)
        return None

    # What the session is serving right now, when we resolved it ourselves. A
    # streamlink-resolved url is not in the variant memory, and the search then
    # simply asks each player type for the session's quality instead.
    match = twitch_playback.variant_for_url(native_url)
    state.searches += 1
    started = time.monotonic()
    try:
        probes = await asyncio.wait_for(
            asyncio.gather(
                *(
                    _probe_player_type(
                        login=login,
                        player_type=player_type,
                        quality=quality,
                        match=match,
                        fetch=fetch,
                        user_token=user_token,
                        device_id=device_id,
                        # A backup is only useful if it is live *now*, so a
                        # search re-mints rather than trusting a cached master
                        # that predates the break - except for the preview tiers,
                        # whose ladders never change.
                        force=player_type not in LOW_QUALITY_PLAYER_TYPES,
                        exact_only=full_quality_only,
                    )
                    for player_type in types
                ),
                return_exceptions=True,
            ),
            timeout=SEARCH_DEADLINE_SECONDS,
        )
    except TimeoutError:
        state.last_attempt_seconds = round(time.monotonic() - started, 3)
        log.info(
            "backup search exceeded its deadline",
            login=login,
            seconds=SEARCH_DEADLINE_SECONDS,
        )
        return None
    finally:
        state.last_attempt_seconds = round(time.monotonic() - started, 3)

    results: list[_Probe] = []
    for player_type, probe in zip(types, probes, strict=True):
        if isinstance(probe, BaseException):
            log.debug(
                "backup probe raised",
                login=login,
                player_type=player_type,
                error=str(probe)[:160],
            )
            state.penalise(player_type, "error", now)
            continue
        if probe.candidate is None:
            if probe.reason and probe.reason != "degraded":
                state.penalise(player_type, probe.reason, now)
            continue
        results.append(probe)

    if not results:
        state.exhausted_until = time.monotonic() + EXHAUSTED_COOLDOWN
        log.info(
            "no clean backup found; every player type is carrying the ad",
            login=login,
            tried=len(types),
            seconds=state.last_attempt_seconds,
            cooldown=EXHAUSTED_COOLDOWN,
        )
        return None

    # Same picture first, then the largest clean one, then the preference order
    # of BACKUP_PLAYER_TYPES.
    best = min(
        results,
        key=lambda p: (not p.exact, -p.pixels, BACKUP_PLAYER_TYPES.index(p.player_type)),
    )
    candidate = best.candidate
    assert candidate is not None
    log.info(
        "clean backup stream found",
        login=login,
        player_type=candidate.player_type,
        quality=candidate.quality,
        same_picture=best.exact,
        tried=len(types),
        seconds=state.last_attempt_seconds,
    )
    return candidate
