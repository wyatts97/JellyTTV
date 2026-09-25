"""Backup-stream ad avoidance.

Technique adapted from TTV-AB by GosuDRM - https://github.com/GosuDRM/TTV-AB
(MIT-based licence with attribution) and from the VAFT / `twitch-videoad`
userscripts. No source was copied; the approach and its constants are
reimplemented here.

The insight this module exists for: Twitch stitches ads **per token**, not per
channel. During a break on the stream being watched, another token for the same
channel is often carrying the live content, so the break can become a seam
instead of a hole.

What decides whether that works is *which* token. Measured live (September
2026), minting a fresh token for `embed`, `popout`, `site` or
`picture-by-picture`:

* the first playlist fetched for it is clean,
* a ~30s preroll is stitched into it a few seconds later,
* and once the preroll has played out, the token carries clean live content.

`autoplay` (the android preview tier, 360p) was never stitched at all.

So a token minted *during* a break - which is what this module used to do,
on every search - passes a first-fetch "clean" check and then serves its own
preroll moments after being spliced in: every hand-over lasted 3-8s before
the next one, with black in between. The search's verdict was worthless
because the one fetch it rested on was the one fetch Twitch never stitches.

What works instead is a **warm pool**: one long-lived token per player type,
minted once and fetched on every poll of the session, like a viewer that
never stops watching. A token plays out its preroll while nothing depends on
it, and is only offered as a backup once it has - "seasoned". A break is then
covered by a token that has already had its ad, the pool keeps judging every
token live, and a hand-over mid-break goes to another token that is clean
*now*, not to a fresh one that will be stitched in a few seconds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.logging_conf import get_logger
from app.services import hls, twitch_playback

log = get_logger(__name__)

# The player types kept warm, in order of preference between otherwise equal
# tokens. `embed` and `popout` carry the whole rendition ladder, so they can
# cover a break at the picture already playing; `autoplay` and
# `picture-by-picture` are preview tiers capped at 360p - a smaller clean
# picture still beats black.
BACKUP_PLAYER_TYPES = (
    "embed",
    "popout",
    "autoplay",
    "picture-by-picture",
)

# Types that only ever offer preview-tier renditions.
LOW_QUALITY_PLAYER_TYPES = frozenset({"autoplay", "picture-by-picture"})

# How long a low-quality backup has to have carried a break before the session
# swaps it for a full-quality token that has become clean, and how many times
# one break may do that. Each swap is a seam and a resolution change.
BRIDGE_HOLD_SECONDS = 6.0
MAX_BRIDGE_UPGRADES = 2

# Bound on one refresh of the pool - minting missing tokens and fetching every
# warm one - so a slow Twitch response can never wedge the session's search
# slot. Comfortably inside a poll interval.
SEARCH_DEADLINE_SECONDS = 5.0

# How long a player type waits before being minted again after its token could
# not be minted or went dead.
COOLDOWNS = {
    "ad-marked": 10.0,
    "stalled": 6.0,
    "not-playable": 2.0,
    "error": 1.5,
}

# A token is trusted to have had its preroll once it has been seen carrying an
# ad and come back, or once it has been warm this long without one - prerolls
# were measured arriving within ~5s of the first fetch, so a token clean for
# this long was simply not given one.
SEASON_SECONDS = 30.0

# Newest segments that must all be live content for a token to be usable. One
# is not enough: a pod is appended at the live edge a segment at a time, and a
# token whose last segment is content but whose next one is an ad is exactly
# the one about to be stitched.
USABLE_TAIL = 2

# Consecutive failed fetches before a warm token is written off and re-minted.
MAX_WARM_FAILURES = 2

# Consecutive clean polls of the native stream before switching back. Matches
# TTV-AB's AD_END_MIN_CLEAN_PLAYLISTS: one clean poll is routinely a gap between
# two pods rather than the end of the break.
MIN_CLEAN_POLLS_TO_RESUME = 3


@dataclass
class BackupCandidate:
    """A usable backup token, and the playlist proving it."""

    player_type: str
    quality: str
    url: str
    playlist: str
    # True when this is a different picture than the session is serving. The
    # session swaps it for a full-quality token as soon as one is usable.
    is_bridge: bool = False
    # When the playlist backing this verdict was fetched (`time.monotonic`).
    found_at: float = field(default_factory=time.monotonic)
    # Whether the token had already had its preroll (see SEASON_SECONDS).
    seasoned: bool = True

    def age(self, now: float) -> float:
        return now - self.found_at


@dataclass
class WarmToken:
    """One long-lived backup token, and what its last fetch said about it."""

    player_type: str
    quality: str
    url: str
    exact: bool
    pixels: int
    is_bridge: bool
    born_at: float
    usable: bool = False
    in_ads: bool = False
    seen_ads: bool = False
    failures: int = 0
    checked_at: float = 0.0
    playlist: str = ""

    def seasoned(self, now: float) -> bool:
        return self.seen_ads or now - self.born_at >= SEASON_SECONDS

    def snapshot(self, now: float) -> dict:
        return {
            "player_type": self.player_type,
            "quality": self.quality,
            "usable": self.usable,
            "in_ads": self.in_ads,
            "seasoned": self.seasoned(now),
            "age_s": round(now - self.born_at, 1),
        }


@dataclass
class BackupState:
    """Per-session bookkeeping for the backup search."""

    active: BackupCandidate | None = None
    cooldowns: dict[str, float] = field(default_factory=dict)
    searching: bool = False
    searches: int = 0
    # The warm pool: one token per player type, kept for the session's life.
    warm: dict[str, WarmToken] = field(default_factory=dict)
    # How many times a token of each type has been caught carrying an ad this
    # session. Ranks tokens that have not proven themselves yet: a type Twitch
    # keeps stitching is the last one to trust with a break.
    stitches: dict[str, int] = field(default_factory=dict)
    # Cost of the last refresh, surfaced in the debug snapshot.
    last_attempt_seconds: float = 0.0

    def available_types(self, exclude: str | None, now: float) -> list[str]:
        """Player types worth minting, best first, minus the native one."""
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
    """Is this playlist free of ad markers *everywhere*?"""
    return not hls.has_ad_markers(playlist)


def accepts(playlist: str) -> tuple[bool, str]:
    """TTV-AB's promotion policy - playable first, then clean."""
    if not is_playable(playlist):
        return False, "not-playable"
    if not is_clean(playlist):
        return False, "ad-marked"
    return True, "clean-playable"


def judge(playlist: str, url: str) -> tuple[bool, bool]:
    """(usable, carrying ads) for one backup playlist.

    Usable means the live edge is content - the newest USABLE_TAIL segments are
    all programme. An ad further back in the window does not disqualify a
    token: it is a preroll or pod that has already played out, in time the
    session has long since served.
    """
    if not is_playable(playlist):
        return False, False
    parsed = hls.parse_media_playlist(playlist, url, strip_ads=True)
    if not parsed.segments:
        return False, False
    tail = parsed.segments[-USABLE_TAIL:]
    tail_is_content = not any(seg.is_ad for seg in tail)
    return tail_is_content, parsed.ad_segment_count > 0 and not tail_is_content


async def _mint(
    state: BackupState,
    *,
    login: str,
    player_type: str,
    quality: str,
    match: twitch_playback.Variant | None,
    user_token: str | None,
    device_id: str | None,
) -> None:
    """Add one player type to the warm pool."""
    now = time.monotonic()
    try:
        master = await twitch_playback.master(
            login,
            player_type,
            user_token=user_token,
            device_id=device_id,
            force=True,
        )
    except twitch_playback.PlaybackError as exc:
        state.penalise(player_type, "error", now)
        log.debug(
            "backup token failed", login=login, player_type=player_type, error=str(exc)[:160]
        )
        return

    variant = twitch_playback.pick_variant(master.variants, quality=quality, match=match)
    if variant is None:
        state.penalise(player_type, "not-playable", now)
        return
    exact = bool(match is not None and variant.matches(match))
    state.warm[player_type] = WarmToken(
        player_type=player_type,
        quality=variant.quality,
        url=variant.url,
        exact=exact,
        pixels=variant.pixels,
        # With nothing to match against, a preview tier is still a degraded
        # picture and must be marked so the session trades up from it.
        is_bridge=(not exact) if match is not None else player_type in LOW_QUALITY_PLAYER_TYPES,
        born_at=now,
    )
    log.info(
        "backup token warming up",
        login=login,
        player_type=player_type,
        quality=variant.quality,
        same_picture=exact,
    )


async def _check(state: BackupState, token: WarmToken, fetch, login: str) -> None:
    """Fetch one warm token and record whether it is usable right now."""
    status, playlist = await fetch(token.url)
    now = time.monotonic()
    token.checked_at = now
    if status != 200 or not playlist:
        token.failures += 1
        token.usable = False
        if token.failures >= MAX_WARM_FAILURES and state.warm.get(token.player_type) is token:
            # A dead variant url means the token expired, not that the type is
            # bad: drop it (and the cached master) and mint a new one.
            del state.warm[token.player_type]
            twitch_playback.invalidate(login, token.player_type)
            state.penalise(token.player_type, "error", now)
            log.info("backup token expired; re-minting", login=login, player_type=token.player_type)
        return
    token.failures = 0
    token.playlist = playlist
    usable, in_ads = judge(playlist, token.url)
    if in_ads and not token.in_ads:
        state.stitches[token.player_type] = state.stitches.get(token.player_type, 0) + 1
        log.info(
            "backup token is carrying an ad",
            login=login,
            player_type=token.player_type,
            age=round(now - token.born_at, 1),
        )
    token.in_ads = in_ads
    token.seen_ads = token.seen_ads or in_ads
    token.usable = usable


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
    """Refresh the warm pool and return the best token usable right now.

    Called on every poll of a session, which is what keeps the pool warm: each
    token's playlist is fetched as a viewer's would be, so its preroll plays
    out long before a break needs it. Tokens are only minted when a type is
    missing - at the start of a session, or after one expired.

    "Best" means: a token that has already had its preroll over one that has
    not (a fresh token is clean for a few seconds and then stitched), then the
    same picture as the native stream, then the largest picture, then the
    preference order of BACKUP_PLAYER_TYPES. The session's active backup is
    never offered as its own replacement.
    """
    now = time.monotonic()
    state.searches += 1
    started = now
    # What the session is serving right now, when we resolved it ourselves. A
    # streamlink-resolved url is not in the variant memory, and the pool then
    # simply asks each player type for the session's quality instead.
    match = twitch_playback.variant_for_url(native_url)
    missing = [pt for pt in state.available_types(native_player_type, now) if pt not in state.warm]
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    _mint(
                        state,
                        login=login,
                        player_type=pt,
                        quality=quality,
                        match=match,
                        user_token=user_token,
                        device_id=device_id,
                    )
                    for pt in missing
                ),
                *(_check(state, token, fetch, login) for token in list(state.warm.values())),
                return_exceptions=True,
            ),
            timeout=SEARCH_DEADLINE_SECONDS,
        )
    except TimeoutError:
        log.info("backup pool refresh exceeded its deadline", login=login)
    finally:
        state.last_attempt_seconds = round(time.monotonic() - started, 3)

    active_url = state.active.url if state.active is not None else None
    now = time.monotonic()
    usable = [
        token
        for token in state.warm.values()
        if token.usable
        and token.url != active_url
        and token.player_type != native_player_type
        and not (full_quality_only and token.is_bridge)
    ]
    if not usable:
        return None

    def rank(token: WarmToken):
        seasoned = token.seasoned(now)
        return (
            not seasoned,
            0 if seasoned else state.stitches.get(token.player_type, 0),
            not token.exact,
            -token.pixels,
            BACKUP_PLAYER_TYPES.index(token.player_type),
        )

    best = min(usable, key=rank)
    return BackupCandidate(
        player_type=best.player_type,
        quality=best.quality,
        url=best.url,
        playlist=best.playlist,
        is_bridge=best.is_bridge,
        found_at=best.checked_at or now,
        seasoned=best.seasoned(now),
    )
