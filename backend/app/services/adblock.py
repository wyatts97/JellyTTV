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

# TTV-AB's fast bridge. `autoplay` is Twitch's Android autoplay tier - capped
# around 360p, and consistently the quickest thing to come back clean, because
# it is the least valuable inventory to stitch an ad into. Reaching for it
# *first* means a break is covered in one probe instead of four, at the cost of
# resolution for a few seconds. `BRIDGE_HOLD_SECONDS` is how long that bridge is
# held before a full-quality candidate is looked for behind it (TTV-AB's
# LQ_HQ_HOLD_MIN_MS).
FAST_BRIDGE_TYPE = "autoplay"
FAST_BRIDGE_QUALITY = "360p"
BRIDGE_HOLD_SECONDS = 8.0

# How many times one break may rotate off a bridge in search of full quality.
# TTV-AB caps the equivalent at 2; past that the seams cost more than the
# resolution buys.
MAX_BRIDGE_UPGRADES = 2

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

# How long to stop searching entirely after a full rotation found nothing clean.
# Without this the search restarted on the very next poll - `active` stays None
# when nothing was found - so a break where every player type carries the ad
# meant a continuous stream of streamlink spawns for the length of the pod.
EXHAUSTED_COOLDOWN = 30.0

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
    # True when this was accepted at a lower quality than the session asked for.
    # The session holds it as a bridge and probes for full quality behind it.
    is_bridge: bool = False


@dataclass
class BackupState:
    """Per-session bookkeeping for the backup search."""

    active: BackupCandidate | None = None
    cooldowns: dict[str, float] = field(default_factory=dict)
    searching: bool = False
    searches: int = 0

    # Remaining (quality, player_type) pairs in the current search round. The
    # rotation is walked one attempt per call rather than in a single nested
    # loop - see `find_backup` for why.
    plan: list[tuple[str, str]] = field(default_factory=list)
    # Set when a whole rotation came back with nothing clean; no new search
    # starts before this.
    exhausted_until: float = 0.0
    # Cost of the last attempt, surfaced in the debug snapshot.
    last_attempt_seconds: float = 0.0

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

    def build_plan(
        self,
        *,
        native_player_type: str | None,
        quality: str,
        now: float,
        full_quality_only: bool = False,
    ) -> list[tuple[str, str]]:
        """Order the rotation, fastest clean stream first.

        The old ordering walked every player type at the session's own quality
        before giving up any resolution, so a break could cost four sequential
        streamlink spawns - one per poll - before anything covered it. Leading
        with the `autoplay`/360p bridge covers the common break on the *first*
        attempt; the session then upgrades behind it (see `MAX_BRIDGE_UPGRADES`).

        `full_quality_only` is that upgrade probe: it wants the session's own
        quality or nothing, because settling for another low rendition would
        just buy a second seam for no picture.
        """
        types = self.available_types(native_player_type, now)
        if not types:
            return []

        if full_quality_only:
            return [(quality, pt) for pt in types if pt != FAST_BRIDGE_TYPE]

        plan: list[tuple[str, str]] = []
        if FAST_BRIDGE_TYPE in types:
            plan.append((FAST_BRIDGE_QUALITY, FAST_BRIDGE_TYPE))
        # Then the session's own quality everywhere else, so a break that a
        # normal player type can cover cleanly is only ever briefly degraded.
        plan += [(quality, pt) for pt in types if pt != FAST_BRIDGE_TYPE]
        # Then give up resolution across the board.
        for fallback in FALLBACK_QUALITIES:
            if fallback == quality:
                continue
            plan += [
                (fallback, pt)
                for pt in types
                if not (pt == FAST_BRIDGE_TYPE and fallback == FAST_BRIDGE_QUALITY)
            ]
        return plan


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
    full_quality_only: bool = False,
) -> BackupCandidate | None:
    """Try **one** backup candidate. Call again next poll to try the next.

    Deliberately a single attempt rather than the whole rotation. This runs while
    a client is waiting for a playlist, and walking every player type at every
    fallback quality in one call meant up to sixteen sequential `streamlink`
    spawns inside a single HTTP request. That is what made a channel carrying
    stitched ads unplayable in Jellyfin's ffmpeg - which gives up - while iOS
    AVPlayer, which keeps retrying the playlist, eventually got through. One
    attempt per poll covers the same ground at the poll cadence and never blocks.

    Returns a candidate that is both playable and clean, or None to mean "not
    this time" - the caller simply asks again.

    `full_quality_only` runs the upgrade probe behind an active bridge: same
    search, but it will not accept another degraded rendition.
    """
    now = time.monotonic()
    if now < state.exhausted_until:
        return None

    if not state.plan:
        state.plan = state.build_plan(
            native_player_type=native_player_type,
            quality=quality,
            now=now,
            full_quality_only=full_quality_only,
        )
        if not state.plan:
            state.exhausted_until = now + EXHAUSTED_COOLDOWN
            log.info("no backup player types available", login=login)
            return None
        state.searches += 1

    attempt_quality, player_type = state.plan.pop(0)
    started = time.monotonic()
    try:
        candidate = await _try_candidate(
            login=login,
            quality=attempt_quality,
            player_type=player_type,
            state=state,
            fetch=fetch,
            user_token=user_token,
            session_quality=quality,
        )
    finally:
        state.last_attempt_seconds = round(time.monotonic() - started, 3)

    if candidate is not None:
        state.plan = []
        log.info(
            "clean backup stream found",
            login=login,
            player_type=candidate.player_type,
            quality=candidate.quality,
            degraded=candidate.quality != quality,
            seconds=state.last_attempt_seconds,
        )
        return candidate

    if not state.plan:
        state.exhausted_until = time.monotonic() + EXHAUSTED_COOLDOWN
        log.info(
            "no clean backup found; every player type is carrying the ad",
            login=login,
            cooldown=EXHAUSTED_COOLDOWN,
        )
    return None


async def _try_candidate(
    *,
    login: str,
    quality: str,
    player_type: str,
    state: BackupState,
    fetch,
    user_token: str | None,
    session_quality: str,
) -> BackupCandidate | None:
    """Resolve and validate one player type. None means "not this one"."""
    if state.cooldowns.get(player_type, 0.0) > time.monotonic():
        return None

    try:
        url = await resolver.resolve_live(
            login,
            quality=quality,
            user_token=user_token,
            player_type=player_type,
            force=True,
            timeout=resolver.BACKUP_RESOLVE_TIMEOUT,
        )
    except resolver.ChannelOffline:
        # The channel itself is gone; no player type will help. Cool the whole
        # rotation rather than walking the rest of it one poll at a time.
        log.info("channel went offline during backup search", login=login)
        state.plan = []
        return None
    except resolver.ResolveError as exc:
        state.penalise(player_type, "error", time.monotonic())
        log.debug(
            "backup resolve failed",
            login=login,
            player_type=player_type,
            error=str(exc)[:160],
        )
        return None

    status, playlist = await fetch(url)
    if status != 200 or not playlist:
        state.penalise(player_type, "error", time.monotonic())
        return None

    ok, reason = accepts(playlist)
    if not ok:
        state.penalise(player_type, reason, time.monotonic())
        log.debug(
            "backup candidate rejected",
            login=login,
            player_type=player_type,
            quality=quality,
            reason=reason,
        )
        return None

    return BackupCandidate(
        player_type=player_type,
        quality=quality,
        url=url,
        playlist=playlist,
        is_bridge=quality != session_quality,
    )
