"""Per-channel streaming sessions that keep the output playlist coherent.

The proxy used to be stateless: every poll independently re-resolved, re-fetched
and re-derived the playlist it handed to ffmpeg. But HLS requires several fields
to be *monotonic across polls* - `#EXT-X-MEDIA-SEQUENCE` names the first segment
in the playlist, `#EXT-X-DISCONTINUITY-SEQUENCE` counts discontinuities that have
already scrolled off - and a stateless rewriter derives all of them from a source
that is not monotonic at all:

* removing ad segments from the head shifts which segment is "first", but the
  upstream sequence number was copied through unchanged, so the number lied;
* Twitch's ad `#EXT-X-DATERANGE` lingers in the sliding window for several polls,
  so the same ad was re-classified and a fresh discontinuity re-inserted each
  time;
* re-resolving produced a different `video-weaver` host with its own sequence
  base and opaque segment URIs, which the player saw as the numbering jumping
  underneath it.

A session fixes all of that by owning the *output*. It assigns each segment a
sequence number exactly once, remembers which segments it has already handed out,
and only ever appends. Upstream's own numbering is never copied through again.

The module takes `resolve` and `fetch` as injected callables so it stays free of
FastAPI and httpx imports and can be unit-tested with plain strings.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.logging_conf import get_logger
from app.services import hls
from app.services.adblock import MIN_CLEAN_POLLS_TO_RESUME, BackupCandidate, BackupState
from app.services.hls import AdRange, OutputSegment, ParsedPlaylist, UpstreamSegment

log = get_logger(__name__)

# How long a segment stays in the "already handed out" map. Deliberately longer
# than the window: a segment can briefly vanish from upstream and come back, and
# re-emitting it under a fresh sequence number is the exact desync we exist to
# prevent.
SEEN_TTL = 120.0
SEEN_MAX = 512

# The floor exists so eviction cannot starve the player. At 3 segments (~6s of
# 2s segments) ffmpeg runs dry as soon as an ad pod is cut out of the window,
# which is the stutter at an ad break. Keeping more history costs a little
# latency behind the live edge and buys continuous playback across a removal.
MIN_WINDOW = 6
MAX_WINDOW = 20
MAX_WINDOW_SECONDS = 90.0

AD_RANGE_TTL = 300.0

# Consecutive ad-only polls after which the EXTINF-title heuristic stops being
# believed for a channel. A real pod ends; a stream whose *name* trips the
# pattern ("Amazon haul unboxing") never does, and would otherwise never play at
# all. This is detection self-correction, not playback throttling - it changes
# what counts as an ad, never how long anything waits.
MAX_CONSECUTIVE_AD_POLLS = 120

# Jellyfin's probe and its ffmpeg both hit the playlist URL; without a short
# render cache they advance the window twice per real poll.
RENDER_CACHE_SECONDS = 1.0

MIN_RESOLVE_INTERVAL = 5.0
MAX_CONSECUTIVE_FAILURES = 3

SESSION_IDLE_SECONDS = 90.0
SESSION_MAX_SECONDS = 6 * 60 * 60

# Upstream statuses that mean "this weaver URL is dead, get a new one".
_DEAD_STATUSES = frozenset({0, 400, 403, 404, 410})

# Takes `prefer_direct`: when True the caller must bypass any ad-avoidance proxy
# and resolve straight from this host. The session flips it after an ad slips
# through, so the retry is minted from a different egress IP than the one that
# just produced an ad - the same polarity flip TTV LOL PRO performs.
Resolver = Callable[[bool], Awaitable[str]]

# Finds the same channel on a different player type, returning a candidate whose
# playlist is verified clean, or None when every type is carrying the ad.
BackupFinder = Callable[[BackupState, str], Awaitable[BackupCandidate | None]]

# Replays ad-progress telemetry for a break that was blocked. Given the raw
# ad-marked playlist; never awaited by the caller.
AdReporter = Callable[[str], Awaitable[int]]

# Kept alive only so the event loop does not garbage-collect a running report
# mid-flight; entries remove themselves on completion.
_ad_report_tasks: set[asyncio.Task] = set()


def _spawn_ad_report(report_ads: AdReporter, text: str) -> None:
    task = asyncio.create_task(report_ads(text))
    _ad_report_tasks.add(task)
    task.add_done_callback(_ad_report_tasks.discard)


# Backup searches run detached from the poll that asked for one, so a slow
# candidate can never hold a playlist response open. Same anti-GC set as above.
_backup_tasks: set[asyncio.Task] = set()


def _start_backup_search(session: StreamSession, backup: BackupFinder) -> None:
    """Kick off one backup attempt in the background, if none is running."""
    if session.backup_task is not None and not session.backup_task.done():
        return
    session.backup.searching = True
    task = asyncio.create_task(backup(session.backup, session.quality))
    session.backup_task = task
    _backup_tasks.add(task)
    task.add_done_callback(_backup_tasks.discard)


def _take_backup_result(session: StreamSession) -> BackupCandidate | None:
    """Collect a finished search, or None while one is still in flight."""
    task = session.backup_task
    if task is None or not task.done():
        return None
    session.backup_task = None
    session.backup.searching = False
    if task.cancelled():
        return None
    exc = task.exception()
    if exc is not None:
        log.warning("backup search failed", login=session.login, error=str(exc)[:200])
        return None
    return task.result()


def _cancel_backup_search(session: StreamSession) -> None:
    if session.backup_task is not None and not session.backup_task.done():
        session.backup_task.cancel()
    session.backup_task = None
    session.backup.searching = False


Fetcher = Callable[[str], Awaitable[tuple[int, str]]]
UriRewriter = Callable[[str], str]


class SessionError(RuntimeError):
    """Upstream could not be made to produce a usable playlist."""


@dataclass(slots=True)
class PlaylistRender:
    text: str
    media_sequence: int
    discontinuity_sequence: int
    segment_count: int
    removed_segments: int = 0
    # This poll was entirely advertising, so no segments were served at all -
    # the playlist is headers only, exactly as the pre-session proxy rendered it.
    ad_pod: bool = False
    # Set while the window is being fed from another player type's stream.
    backup_player_type: str | None = None
    from_cache: bool = False


@dataclass
class SessionStats:
    polls: int = 0
    resolves: int = 0
    # Polls where the entire upstream window was advertising and we served no
    # segments. Rises during a break and stops after it - a channel where this
    # never stops is one the title heuristic is misreading.
    ad_pod_polls: int = 0
    # Times we went and fetched a different stream because an ad appeared.
    ad_replacements: int = 0
    # Polls served from a backup stream instead of dead air.
    backup_polls: int = 0
    removed_segments: int = 0
    repeated_renders: int = 0


@dataclass
class StreamSession:
    key: str
    login: str
    quality: str

    upstream_url: str | None = None
    generation: int = 0

    next_seq: int = 0
    discontinuity_seq: int = 0
    pending_discontinuity: bool = False

    window: deque[OutputSegment] = field(default_factory=deque)
    seen: OrderedDict[str, tuple[int, float]] = field(default_factory=OrderedDict)
    ad_ranges: dict[str, AdRange] = field(default_factory=dict)

    # Length of the current run of ad-only polls, and whether the EXTINF-title
    # heuristic is still believed for this channel.
    consecutive_ad_polls: int = 0
    trust_titles: bool = True

    # Ad-replacement state. `prefer_direct_resolve` is the polarity flip: an ad
    # that arrives over the proxy is retried direct and vice versa, because what
    # changes the outcome is the egress IP Twitch mints the token for.
    prefer_direct_resolve: bool = False
    ad_replacements: int = 0

    # TTV-AB backup substitution. `serving_backup` says the window is currently
    # being fed from another player type; `clean_native_polls` counts how long
    # the real stream has looked clean, so we do not switch back on the gap
    # between two pods of the same break.
    backup: BackupState = field(default_factory=BackupState)
    backup_task: asyncio.Task | None = field(default=None, repr=False)
    serving_backup: bool = False
    clean_native_polls: int = 0

    target_duration: float = 2.0
    version: int = 3
    passthrough_tags: list[str] = field(default_factory=list)

    last_rendered: str | None = None
    last_render_at: float = 0.0
    last_media_sequence: int = 0
    last_upstream_tail: str | None = None
    last_change_at: float = 0.0

    consecutive_failures: int = 0
    # Tracked separately from the initial resolve: the backoff exists to stop a
    # flapping upstream respawning streamlink in a tight loop, and must not
    # suppress the very first recovery attempt of a session.
    last_forced_resolve_at: float = 0.0

    is_master: bool | None = None

    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    stats: SessionStats = field(default_factory=SessionStats)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def snapshot(self, now: float | None = None) -> dict:
        now = now if now is not None else time.monotonic()
        return {
            "exists": True,
            "login": self.login,
            "quality": self.quality,
            "upstream_url": self.upstream_url,
            "generation": self.generation,
            "age_s": round(now - self.created_at, 1),
            "idle_s": round(now - self.last_access, 1),
            "next_seq": self.next_seq,
            "media_sequence": self.last_media_sequence,
            "discontinuity_sequence": self.discontinuity_seq,
            "window_len": len(self.window),
            "seen_len": len(self.seen),
            "target_duration": self.target_duration,
            "consecutive_failures": self.consecutive_failures,
            "known_ad_ranges": sorted(self.ad_ranges),
            # `trust_titles: false` means the EXTINF-title heuristic matched
            # endlessly on this channel and was revoked - the first place to look
            # if a channel would not play at all and then started working.
            "trust_titles": self.trust_titles,
            "consecutive_ad_polls": self.consecutive_ad_polls,
            # Which side of the proxy the current upstream was minted from, and
            # how many times an ad forced us to switch. Both rising steadily
            # means neither egress is coming back ad-free.
            "prefer_direct_resolve": self.prefer_direct_resolve,
            "ad_replacements": self.ad_replacements,
            # Which player type is currently filling the break, if any, and how
            # long native has looked clean - the two numbers that say whether
            # backup substitution is working on this channel.
            "serving_backup": self.serving_backup,
            "backup_player_type": self.backup.active.player_type if self.backup.active else None,
            "backup_quality": self.backup.active.quality if self.backup.active else None,
            "clean_native_polls": self.clean_native_polls,
            # A search that is still in flight, and what the last attempt cost.
            # A rising `backup_last_attempt_s` is the tell for a slow player
            # type; `backup_searching` stuck true means a search is wedged.
            "backup_searching": self.backup.searching,
            "backup_last_attempt_s": self.backup.last_attempt_seconds,
            "backup_exhausted": self.backup.exhausted_until > time.monotonic(),
            "stats": vars(self.stats),
        }


_sessions: dict[str, StreamSession] = {}
_registry_lock = asyncio.Lock()


def session_key(login: str, quality: str, variant: str | None = None) -> str:
    return f"{login}:{quality}:{variant}" if variant else f"{login}:{quality}"


# ------------------------------------------------------------ segment identity
def _normalise_pdt(epoch: float) -> int:
    """Round to 100ms so formatting drift between weaver nodes cancels out."""
    return int(round(epoch * 10))


def segment_key(seg: UpstreamSegment, generation: int) -> str:
    """A segment identity that survives a weaver switch.

    After switching weaver hosts the same wall-clock segment arrives under a
    completely different opaque URL, so URL-based dedupe fails exactly when it
    matters most. Twitch emits `#EXT-X-PROGRAM-DATE-TIME` per segment on live
    playlists, so the timestamp is the normal path and the switch becomes
    invisible to the client.
    """
    if seg.program_date_epoch is not None:
        return f"pdt:{_normalise_pdt(seg.program_date_epoch)}"
    name = urlsplit(seg.uri).path.rsplit("/", 1)[-1]
    if name:
        return f"name:{name}"
    return f"gen:{generation}:{seg.upstream_seq}"


# ------------------------------------------------------------------- advancing
def _prune_seen(session: StreamSession, now: float) -> None:
    expired = [k for k, (_seq, exp) in session.seen.items() if exp <= now]
    for key in expired:
        session.seen.pop(key, None)
    while len(session.seen) > SEEN_MAX:
        session.seen.popitem(last=False)


def _prune_ad_ranges(session: StreamSession, now: float) -> None:
    stale = [k for k, r in session.ad_ranges.items() if now - r.first_seen > AD_RANGE_TTL]
    for key in stale:
        session.ad_ranges.pop(key, None)


def _evict(session: StreamSession) -> None:
    """Trim the window, counting discontinuities that scroll off the front.

    RFC 8216 6.2.1: DISCONTINUITY-SEQUENCE counts the discontinuities that have
    already left the playlist. Getting this wrong is why players lost their place
    after an ad break.
    """
    while len(session.window) > MAX_WINDOW or (
        len(session.window) > MIN_WINDOW
        and sum(s.duration for s in session.window) > MAX_WINDOW_SECONDS
    ):
        popped = session.window.popleft()
        if popped.discontinuity:
            session.discontinuity_seq += 1


def _request_ad_replacement(session: StreamSession) -> None:
    """Go get a different stream rather than waiting the ad break out.

    An ad is baked in when Twitch mints the playback token, so a fresh token
    from a different egress often comes back without one - this is how TTV LOL
    PRO recovers, and it turns a break from "dead air for its full duration"
    into a brief gap. Dropping `upstream_url` makes the next poll re-resolve;
    flipping `prefer_direct_resolve` makes that resolve come from the other side
    of the proxy than the one that just served an ad.

    Fires once per break. Retrying every poll would respawn streamlink several
    times a second for the length of a midroll, and the extension caps itself
    the same way for the same reason.
    """
    if session.consecutive_ad_polls != 1:
        return
    session.upstream_url = None
    session.prefer_direct_resolve = not session.prefer_direct_resolve
    session.ad_replacements += 1
    session.stats.ad_replacements += 1
    log.info(
        "ad detected; re-resolving from the other egress",
        login=session.login,
        prefer_direct=session.prefer_direct_resolve,
    )


def _distrust_titles_if_endless(session: StreamSession, parsed: ParsedPlaylist) -> None:
    """Stop believing the title heuristic on a channel it never stops matching.

    Detection self-correction, not playback control. A real pod ends after a
    couple of minutes; a channel whose *name* contains "amazon" matches on every
    segment forever and would never play at all. Only fires when nothing
    corroborated the call - if a real ad daterange was ever seen, a long break
    is just a long break.
    """
    if session.consecutive_ad_polls < MAX_CONSECUTIVE_AD_POLLS:
        return
    if session.ad_ranges:
        return
    if not all(s.ad_source == "title" for s in parsed.segments if s.is_ad):
        return

    session.trust_titles = False
    session.consecutive_ad_polls = 0
    log.warning(
        "ad title heuristic matched every segment with no corroborating "
        "daterange; disabling it for this session",
        login=session.login,
        polls=MAX_CONSECUTIVE_AD_POLLS,
    )


def _advance(
    session: StreamSession,
    parsed: ParsedPlaylist,
    *,
    strip_ads: bool,
    rewrite_uri: UriRewriter | None,
    now: float,
) -> tuple[int, bool]:
    """Fold one poll into the session. Returns (removed_segments, ad_pod)."""
    rewriter = rewrite_uri or (lambda u: u)

    # Target duration must never shrink: a mid-session drop makes players
    # re-evaluate their buffer and stall.
    session.target_duration = max(session.target_duration, parsed.target_duration)
    if parsed.version:
        session.version = max(session.version, parsed.version)
    if parsed.passthrough_tags:
        session.passthrough_tags = list(parsed.passthrough_tags)

    for rng in parsed.ad_ranges:
        session.ad_ranges.setdefault(rng.id, rng).first_seen = now
    _prune_ad_ranges(session, now)

    kept = [s for s in parsed.segments if not s.is_ad] if strip_ads else list(parsed.segments)
    removed = len(parsed.segments) - len(kept)

    ad_pod = bool(strip_ads and parsed.segments and not kept)
    if ad_pod and session.next_seq == 0:
        # Nothing has ever been handed out, so an empty playlist would be the
        # very first thing the player sees - and ffmpeg cannot probe a playlist
        # with no segments, so it fails outright instead of waiting the pod out.
        # (A more patient player, like iOS AVPlayer, keeps polling and recovers -
        # which is exactly why a channel with a preroll played in Streamyfin and
        # not in the Jellyfin web UI.) The rule `hls.rewrite_playlist` already
        # applies holds here: an unstripped ad is merely annoying, an empty
        # playlist is fatal. Pass this one pod through; stripping resumes on the
        # next poll, by which point the session has a window to hold instead.
        log.info(
            "ad pod at session start - passing it through rather than serving nothing",
            login=session.login,
            segments=len(parsed.segments),
        )
        kept = list(parsed.segments)
        removed = 0
        ad_pod = False

    if ad_pod:
        # Every segment upstream is advertising, so nothing is served - the
        # caller renders a headers-only playlist, which is exactly what the
        # pre-session proxy did. No segment is ever passed through and no
        # previous window is re-served: an ad must not reach the player, and
        # re-serving stale segments only makes ffmpeg wait silently instead of
        # noticing and rejoining at the live edge.
        session.consecutive_ad_polls += 1
        session.stats.ad_pod_polls += 1
        session.pending_discontinuity = True
        _distrust_titles_if_endless(session, parsed)
        _request_ad_replacement(session)
        log.info(
            "ad pod - serving no segments",
            login=session.login,
            segments=len(parsed.segments),
            consecutive=session.consecutive_ad_polls,
            prefer_direct=session.prefer_direct_resolve,
        )

    prev_index: int | None = None
    for seg in kept:
        key = segment_key(seg, session.generation)
        if key in session.seen:
            prev_index = seg.index
            continue

        # Real content resumed, so the ad-only run is over.
        session.consecutive_ad_polls = 0

        # A hole in the upstream indices means we removed an ad pod here.
        gap = prev_index is not None and seg.index != prev_index + 1
        discontinuity = seg.discontinuity_before or gap or session.pending_discontinuity
        session.pending_discontinuity = False

        out = OutputSegment(
            seq=session.next_seq,
            uri=rewriter(seg.uri),
            duration=seg.duration,
            key=key,
            title=seg.title,
            program_date_time=seg.program_date_time,
            byterange=seg.byterange,
            discontinuity=discontinuity,
        )
        session.next_seq += 1
        session.window.append(out)
        session.seen[key] = (out.seq, now + SEEN_TTL)
        prev_index = seg.index

    _evict(session)
    _prune_seen(session, now)
    return removed, ad_pod


async def _apply_backup(
    session: StreamSession,
    backup: BackupFinder,
    *,
    ad_pod: bool,
    parsed: ParsedPlaylist,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None,
    now: float,
) -> bool:
    """Splice a clean backup stream over an ad break. Returns the new ad_pod.

    This is the whole point of the TTV-AB strategy: an ad is stitched per token,
    so the same channel on another player type is usually still carrying the
    live content. Feeding those segments into the window turns a break from dead
    air into a seam, and `_advance` gives them monotonic numbering for free.
    """
    if not ad_pod:
        # Native is clean. Wait for it to stay that way before switching back:
        # one clean poll is routinely the gap between two pods of one break.
        if session.serving_backup:
            session.clean_native_polls += 1
            if session.clean_native_polls < MIN_CLEAN_POLLS_TO_RESUME:
                return await _serve_backup(session, fetch, rewrite_uri, now)
            log.info(
                "ad break over; returning to the native stream",
                login=session.login,
                clean_polls=session.clean_native_polls,
                backup=session.backup.active.player_type if session.backup.active else None,
            )
            session.serving_backup = False
            session.clean_native_polls = 0
            _cancel_backup_search(session)
            session.backup.clear()
            session.pending_discontinuity = True
        return False

    session.clean_native_polls = 0
    if session.backup.active is None:
        # Never awaited inline: a search resolves through streamlink, and doing
        # that while the client waits for this playlist is what made an ad break
        # look like a dead channel. Start one, serve what we can now, and pick
        # the result up on a later poll.
        candidate = _take_backup_result(session)
        if candidate is None:
            _start_backup_search(session, backup)
            # No backup yet, so this poll still emits nothing - but it returns
            # immediately instead of holding the request open.
            return True
        session.backup.active = candidate
        session.serving_backup = True
        session.pending_discontinuity = True
        log.info(
            "splicing backup stream over the ad break",
            login=session.login,
            player_type=candidate.player_type,
            quality=candidate.quality,
        )

    return await _serve_backup(session, fetch, rewrite_uri, now)


async def _serve_backup(
    session: StreamSession,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None,
    now: float,
) -> bool:
    """Poll the active backup and fold its newest segments in.

    Re-validated on every poll rather than trusted once: a backup can start
    carrying the ad itself part-way through a break, and TTV-AB re-checks
    continuously for the same reason. A backup that goes bad is dropped and
    cooled down so the next poll picks a different player type.
    """
    active = session.backup.active
    if active is None:
        return True

    status, playlist = await fetch(active.url)
    if status != 200 or not playlist:
        session.backup.penalise(active.player_type, "error", now)
        session.backup.clear()
        return True

    from app.services import adblock

    ok, reason = adblock.accepts(playlist)
    if not ok:
        log.info(
            "backup stream went bad; rotating",
            login=session.login,
            player_type=active.player_type,
            reason=reason,
        )
        session.backup.penalise(active.player_type, reason, now)
        session.backup.clear()
        return True

    parsed = hls.parse_media_playlist(
        playlist, active.url, strip_ads=True, now=now, trust_titles=session.trust_titles
    )
    if not parsed.segments:
        session.backup.penalise(active.player_type, "not-playable", now)
        session.backup.clear()
        return True

    _advance(session, parsed, strip_ads=True, rewrite_uri=rewrite_uri, now=now)
    session.stats.backup_polls += 1
    return False


def _render(session: StreamSession, *, ad_pod: bool = False) -> PlaylistRender:
    """Render the current window, or a headers-only playlist during an ad pod.

    An ad pod renders no segments at all rather than re-serving the window. The
    window's segments have already been played, so re-serving them only tells
    ffmpeg "nothing new yet" indefinitely; an empty playlist lets it notice and
    rejoin at the live edge once the break ends. `MEDIA-SEQUENCE` is held at its
    current value so numbering stays monotonic across the gap.
    """
    if ad_pod:
        return PlaylistRender(
            text=hls.render_media_playlist(
                [],
                target_duration=session.target_duration,
                media_sequence=session.last_media_sequence,
                discontinuity_sequence=session.discontinuity_seq,
                version=session.version,
                passthrough_tags=session.passthrough_tags,
            ),
            media_sequence=session.last_media_sequence,
            discontinuity_sequence=session.discontinuity_seq,
            segment_count=0,
            ad_pod=True,
        )

    media_sequence = session.window[0].seq if session.window else session.last_media_sequence
    # Monotonicity is structural (append-right / pop-left over increasing seq),
    # but clamp anyway so a future bug degrades into a stall rather than a reset.
    if media_sequence < session.last_media_sequence:
        log.warning(
            "media sequence would move backwards; clamping",
            login=session.login,
            computed=media_sequence,
            previous=session.last_media_sequence,
        )
        media_sequence = session.last_media_sequence
    session.last_media_sequence = media_sequence

    text = hls.render_media_playlist(
        list(session.window),
        target_duration=session.target_duration,
        media_sequence=media_sequence,
        discontinuity_sequence=session.discontinuity_seq,
        version=session.version,
        passthrough_tags=session.passthrough_tags,
    )
    return PlaylistRender(
        text=text,
        media_sequence=media_sequence,
        discontinuity_sequence=session.discontinuity_seq,
        segment_count=len(session.window),
    )


# --------------------------------------------------------------- upstream loop
def _stale_after(session: StreamSession) -> float:
    return max(15.0, 4.0 * session.target_duration)


async def _acquire_upstream(
    session: StreamSession, resolve: Resolver, now: float, *, force: bool = False
) -> str:
    if session.upstream_url and not force:
        return session.upstream_url
    if force:
        if session.upstream_url and now - session.last_forced_resolve_at < MIN_RESOLVE_INTERVAL:
            # Already re-resolved moments ago; hammering streamlink will not help.
            return session.upstream_url
        session.last_forced_resolve_at = now
    session.stats.resolves += 1
    url = await resolve(session.prefer_direct_resolve)
    if force and session.upstream_url and url != session.upstream_url:
        session.generation += 1
    session.upstream_url = url
    return url


async def _poll_upstream(
    session: StreamSession,
    *,
    resolve: Resolver,
    fetch: Fetcher,
    strip_ads: bool,
    now: float,
    report_ads: AdReporter | None = None,
) -> ParsedPlaylist:
    """Fetch and parse, re-resolving when the current weaver URL is unusable."""
    last_status = 0
    for attempt in range(3):
        url = await _acquire_upstream(session, resolve, now, force=attempt > 0)
        status, text = await fetch(url)
        last_status = status

        if status in _DEAD_STATUSES:
            log.info(
                "upstream playlist unusable, re-resolving",
                login=session.login,
                status=status,
                attempt=attempt,
            )
            continue
        if status >= 500:
            # Transient: the weaver is probably fine, retry the same URL once.
            if attempt == 0:
                continue
            break

        parsed = hls.parse_media_playlist(
            text,
            url,
            strip_ads=strip_ads,
            known_ad_ranges=session.ad_ranges,
            now=now,
            trust_titles=session.trust_titles,
        )
        if report_ads is not None and parsed.ad_ranges:
            # Fire-and-forget: this is telemetry for Twitch's benefit and must
            # never delay a playlist or fail a poll.
            _spawn_ad_report(report_ads, text)
        if not parsed.segments:
            log.info("upstream playlist had no segments, re-resolving", login=session.login)
            continue

        # A weaver that answers 200 but never advances is a failure mode the old
        # stateless proxy could not even see.
        tail = segment_key(parsed.segments[-1], session.generation)
        if tail != session.last_upstream_tail:
            session.last_upstream_tail = tail
            session.last_change_at = now
        elif now - session.last_change_at > _stale_after(session) and attempt == 0:
            log.info("upstream playlist frozen, re-resolving", login=session.login)
            continue

        if parsed.has_endlist:
            session.upstream_url = None

        session.consecutive_failures = 0
        return parsed

    session.consecutive_failures += 1
    raise SessionError(f"upstream playlist unavailable (last status {last_status})")


# ------------------------------------------------------------------ public api
async def _get_or_create(key: str, login: str, quality: str) -> StreamSession:
    async with _registry_lock:
        session = _sessions.get(key)
        if session is None:
            session = StreamSession(key=key, login=login, quality=quality)
            _sessions[key] = session
        return session


async def get_playlist(
    *,
    login: str,
    quality: str,
    strip_ads: bool,
    resolve: Resolver,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None = None,
    variant: str | None = None,
    backup: BackupFinder | None = None,
    report_ads: AdReporter | None = None,
) -> PlaylistRender:
    """Return the client-facing playlist for this channel, advancing the session.

    `backup` enables TTV-AB-style substitution: when the whole upstream window is
    advertising, it goes and finds the same channel on a different player type
    and that stream is spliced in, so a break costs a seam rather than its full
    duration. Without it an ad pod still means no output.
    """
    key = session_key(login, quality, variant)
    session = await _get_or_create(key, login, quality)

    async with session.lock:
        now = time.monotonic()
        session.last_access = now

        if session.last_rendered and now - session.last_render_at < RENDER_CACHE_SECONDS:
            session.stats.repeated_renders += 1
            return PlaylistRender(
                text=session.last_rendered,
                media_sequence=session.last_media_sequence,
                discontinuity_sequence=session.discontinuity_seq,
                segment_count=len(session.window),
                from_cache=True,
            )

        try:
            parsed = await _poll_upstream(
                session,
                resolve=resolve,
                fetch=fetch,
                strip_ads=strip_ads,
                now=now,
                report_ads=report_ads,
            )
        except SessionError:
            # A repeated playlist is legal HLS and tells the player "nothing new
            # yet" - far better than an error, as long as we ever had output.
            if session.last_rendered and session.consecutive_failures <= MAX_CONSECUTIVE_FAILURES:
                session.stats.repeated_renders += 1
                return PlaylistRender(
                    text=session.last_rendered,
                    media_sequence=session.last_media_sequence,
                    discontinuity_sequence=session.discontinuity_seq,
                    segment_count=len(session.window),
                    from_cache=True,
                )
            raise

        session.stats.polls += 1
        removed, ad_pod = _advance(
            session, parsed, strip_ads=strip_ads, rewrite_uri=rewrite_uri, now=now
        )
        session.stats.removed_segments += removed

        if backup is not None:
            ad_pod = await _apply_backup(
                session,
                backup,
                ad_pod=ad_pod,
                parsed=parsed,
                fetch=fetch,
                rewrite_uri=rewrite_uri,
                now=now,
            )

        render = _render(session, ad_pod=ad_pod)
        render.removed_segments = removed
        render.backup_player_type = (
            session.backup.active.player_type
            if session.backup.active and session.serving_backup
            else None
        )
        session.last_rendered = render.text
        session.last_render_at = now
        return render


def touch(login: str, quality: str, variant: str | None = None) -> None:
    """Keep a session alive while a client is pulling segments from it."""
    session = _sessions.get(session_key(login, quality, variant))
    if session is not None:
        session.last_access = time.monotonic()


def get(login: str, quality: str, variant: str | None = None) -> StreamSession | None:
    return _sessions.get(session_key(login, quality, variant))


async def ensure(login: str, quality: str, variant: str | None = None) -> StreamSession:
    """Get the session for this key, creating it if it does not exist yet.

    Lets a caller record something on the session (notably `is_master`) before
    the first playlist has been served through it.
    """
    return await _get_or_create(session_key(login, quality, variant), login, quality)


def drop(login: str, quality: str, variant: str | None = None) -> None:
    session = _sessions.pop(session_key(login, quality, variant), None)
    if session is not None:
        _cancel_backup_search(session)


def reset() -> None:
    """Drop every session. Used by tests and by settings changes."""
    for session in _sessions.values():
        _cancel_backup_search(session)
    _sessions.clear()


def stats() -> list[dict]:
    return [s.snapshot() for s in _sessions.values()]


def sweep(now: float | None = None) -> int:
    now = now if now is not None else time.monotonic()
    dead = [
        key
        for key, s in _sessions.items()
        if now - s.last_access > SESSION_IDLE_SECONDS
        or now - s.created_at > SESSION_MAX_SECONDS
    ]
    for key in dead:
        session = _sessions.pop(key, None)
        if session is not None:
            _cancel_backup_search(session)
    if dead:
        log.debug("swept idle stream sessions", count=len(dead))
    return len(dead)


async def sweeper_task(interval: float = 30.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the sweeper must never die
            log.exception("stream session sweeper failed")


async def preview(
    *,
    login: str,
    quality: str,
    strip_ads: bool,
    resolve: Resolver,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None = None,
    variant: str | None = None,
) -> dict:
    """Run the algorithm against a copy of the session, changing nothing.

    This is what the debug endpoint uses, so inspecting a live stream can never
    perturb the playback it is meant to explain.
    """
    key = session_key(login, quality, variant)
    live = _sessions.get(key)
    session = StreamSession(key=key, login=login, quality=quality)
    if live is not None:
        session = _clone(live)

    now = time.monotonic()
    url = await _acquire_upstream(session, resolve, now)
    status, text = await fetch(url)
    parsed = hls.parse_media_playlist(
        text,
        url,
        strip_ads=strip_ads,
        known_ad_ranges=session.ad_ranges,
        now=now,
        trust_titles=session.trust_titles,
    )
    before = live.snapshot(now) if live is not None else {"exists": False}

    removed, ad_pod = _advance(
        session, parsed, strip_ads=strip_ads, rewrite_uri=rewrite_uri, now=now
    )
    render = _render(session, ad_pod=ad_pod)

    return {
        "login": login,
        "quality": quality,
        "upstream_url": url,
        "session": before,
        "upstream": {
            "status": status,
            "media_sequence": parsed.media_sequence,
            "target_duration": parsed.target_duration,
            "segment_count": len(parsed.segments),
            "ad_segments": parsed.ad_segment_count,
            "low_latency": parsed.is_low_latency,
            "dropped_tags": parsed.dropped_tags,
            "prefetch_uris": parsed.prefetch_uris,
            "has_endlist": parsed.has_endlist,
        },
        "segments": [
            {
                "index": s.index,
                "upstream_seq": s.upstream_seq,
                "key": segment_key(s, session.generation),
                "pdt": s.program_date_time,
                "duration": s.duration,
                "is_ad": s.is_ad,
                "ad_source": s.ad_source,
                "already_emitted": segment_key(s, session.generation)
                in (live.seen if live is not None else {}),
            }
            for s in parsed.segments
        ],
        "result": {
            "removed_segments": removed,
            "ad_pod": ad_pod,
            "media_sequence": render.media_sequence,
            "discontinuity_sequence": render.discontinuity_sequence,
            "segment_count": render.segment_count,
        },
        "raw": text,
        "rewritten": render.text,
    }


def _clone(session: StreamSession) -> StreamSession:
    """Deep-copy a session without its asyncio.Lock (which cannot be copied)."""
    clone = StreamSession(key=session.key, login=session.login, quality=session.quality)
    clone.upstream_url = session.upstream_url
    clone.generation = session.generation
    clone.next_seq = session.next_seq
    clone.discontinuity_seq = session.discontinuity_seq
    clone.pending_discontinuity = session.pending_discontinuity
    clone.window = deque(copy.deepcopy(list(session.window)))
    clone.seen = OrderedDict(session.seen)
    clone.ad_ranges = copy.deepcopy(session.ad_ranges)
    clone.consecutive_ad_polls = session.consecutive_ad_polls
    clone.trust_titles = session.trust_titles
    clone.prefer_direct_resolve = session.prefer_direct_resolve
    clone.ad_replacements = session.ad_replacements
    clone.target_duration = session.target_duration
    clone.version = session.version
    clone.passthrough_tags = list(session.passthrough_tags)
    clone.last_rendered = session.last_rendered
    clone.last_media_sequence = session.last_media_sequence
    clone.last_upstream_tail = session.last_upstream_tail
    clone.last_change_at = session.last_change_at
    clone.is_master = session.is_master
    return clone
