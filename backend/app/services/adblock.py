"""Backup-stream ad avoidance, ported from TTV-AB.

Technique adapted from TTV-AB by GosuDRM - https://github.com/GosuDRM/TTV-AB
(MIT-based licence with attribution). No source was copied; the approach and its
constants are reimplemented here.

The insight this module exists for: Twitch stitches ads **per token**, not per
channel. A playback token minted for a different `playerType` on the same
channel usually comes back *clean*, carrying the same live content at the same
moment. So an ad break does not have to mean dead air - there is another copy of
the stream to switch to, and the break becomes a seam instead of a hole.

Every other approach in this codebase accepted that hole because it assumed
Twitch stops sending the broadcaster's video during a break. It does not; it
stops sending it *to that token*.

Backups are acquired through `resolver.resolve_live(player_type=...)` rather
than by minting tokens here. Delegating to streamlink keeps the part most
exposed to Twitch changing its API - the GraphQL query, its persisted-query
hash, the usher dance - as somebody else's maintenance burden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.logging_conf import get_logger
from app.services import hls, resolver

log = get_logger(__name__)

# TTV-AB's player types. `site` is what a normal viewer uses and is therefore
# the one most likely to be carrying the ad we are trying to escape, so it sits
# last. `autoplay` is the fast one - it is what TTV-AB reaches for first when
# low-quality fallback is allowed.
BACKUP_PLAYER_TYPES = ("embed", "popout", "mobile_web", "autoplay", "site")

# Ad-avoidance strategies, in the order they are offered.
STRATEGY_TTV_AB = "ttv_ab"
STRATEGY_TTV_LOL_PRO = "ttv_lol_pro"
STRATEGY_STRIP_ONLY = "strip_only"
STRATEGIES = (STRATEGY_TTV_AB, STRATEGY_TTV_LOL_PRO, STRATEGY_STRIP_ONLY)
DEFAULT_STRATEGY = STRATEGY_TTV_AB


def configured_strategy(value: str | None) -> str:
    """Resolve the stored setting, tolerating the NULL an upgrade leaves."""
    strategy = (value or "").strip()
    return strategy if strategy in STRATEGIES else DEFAULT_STRATEGY

# Qualities to try when the stream's own quality yields nothing clean. 360p is
# TTV-AB's floor: below that Twitch renditions get too degraded to be worth
# switching to.
FALLBACK_QUALITIES = ("720p", "480p", "360p")

# How long a player type stays out of the rotation after failing, by reason.
# An ad-marked type is likely to stay ad-marked for the length of the pod, so it
# waits longest; a transport error is probably transient.
COOLDOWNS = {
    "ad-marked": 15.0,
    "stalled": 10.0,
    "not-playable": 2.0,
    "error": 1.5,
}

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


@dataclass
class BackupState:
    """Per-session bookkeeping for the backup search."""

    active: BackupCandidate | None = None
    cooldowns: dict[str, float] = field(default_factory=dict)
    searching: bool = False
    searches: int = 0

    def available_types(self, exclude: str | None, now: float) -> list[str]:
        """Player types worth trying, in TTV-AB's order, minus the native one."""
        return [
            pt
            for pt in BACKUP_PLAYER_TYPES
            if pt != exclude and self.cooldowns.get(pt, 0.0) <= now
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


async def find_backup(
    *,
    login: str,
    quality: str,
    native_player_type: str | None,
    state: BackupState,
    fetch,
    user_token: str | None = None,
    allow_low_quality: bool = True,
) -> BackupCandidate | None:
    """Search player types for one serving a clean playlist for this channel.

    Tries the stream's own quality across every eligible player type before
    dropping quality, so a break costs resolution only when it has to. Returns
    the first candidate that is both playable and clean, or None.
    """
    now = time.monotonic()
    candidates = state.available_types(native_player_type, now)
    if not candidates:
        log.info("no backup player types available", login=login)
        return None

    qualities = [quality]
    if allow_low_quality:
        qualities += [q for q in FALLBACK_QUALITIES if q != quality]

    state.searches += 1
    for attempt_quality in qualities:
        for player_type in candidates:
            if state.cooldowns.get(player_type, 0.0) > time.monotonic():
                continue
            try:
                url = await resolver.resolve_live(
                    login,
                    quality=attempt_quality,
                    user_token=user_token,
                    player_type=player_type,
                    force=True,
                )
            except resolver.ChannelOffline:
                # The channel itself is gone; no player type will help.
                log.info("channel went offline during backup search", login=login)
                return None
            except resolver.ResolveError as exc:
                state.penalise(player_type, "error", time.monotonic())
                log.debug(
                    "backup resolve failed",
                    login=login,
                    player_type=player_type,
                    error=str(exc)[:160],
                )
                continue

            status, playlist = await fetch(url)
            if status != 200 or not playlist:
                state.penalise(player_type, "error", time.monotonic())
                continue

            ok, reason = accepts(playlist)
            if not ok:
                state.penalise(player_type, reason, time.monotonic())
                log.debug(
                    "backup candidate rejected",
                    login=login,
                    player_type=player_type,
                    quality=attempt_quality,
                    reason=reason,
                )
                continue

            log.info(
                "clean backup stream found",
                login=login,
                player_type=player_type,
                quality=attempt_quality,
                degraded=attempt_quality != quality,
            )
            return BackupCandidate(
                player_type=player_type,
                quality=attempt_quality,
                url=url,
                playlist=playlist,
            )

    log.info(
        "no clean backup found; every player type is carrying the ad",
        login=login,
        tried=len(candidates),
    )
    return None
