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
from app.services.adblock import (
    BRIDGE_HOLD_SECONDS,
    MAX_BRIDGE_UPGRADES,
    MIN_CLEAN_POLLS_TO_RESUME,
    BackupCandidate,
    BackupState,
)
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

# Consecutive fetch failures before a backup is dropped. One is a hiccup.
MAX_BACKUP_FAILURES = 2

# The spare: the warm pool's best token for covering a break right now (see
# services.adblock). The pool is refreshed on every poll, so the spare always
# reflects what each token's playlist said moments ago, and neither the start
# of a break nor a hand-over mid-break has to wait for a search.

# How far behind the served timeline a source's newest segment may be before
# its clock is assumed not to agree with ours and the timeline is re-based on
# it. Twitch stamps PROGRAM-DATE-TIME at the transcoder, so copies of one
# channel agree to well under a second; this only guards against a source that
# does not, which would otherwise have every segment skipped as already covered.
CLOCK_RESET_SECONDS = 20.0

MIN_RESOLVE_INTERVAL = 5.0
MAX_CONSECUTIVE_FAILURES = 3

# How far the wall clock may drift from the media timeline between two adjacent
# segments before we call it a real hole. Segment durations and PROGRAM-DATE-TIME
# never line up exactly, so this has to absorb ordinary jitter while still
# catching a cut - and a cut is at minimum a whole segment, so half a second is
# comfortably inside the gap it needs to find.
PDT_GAP_TOLERANCE = 0.5

SESSION_IDLE_SECONDS = 90.0
SESSION_MAX_SECONDS = 6 * 60 * 60

# Upstream statuses that mean "this weaver URL is dead, get a new one".
_DEAD_STATUSES = frozenset({0, 400, 403, 404, 410})

# Re-acquires an upstream url for this channel. Called only when the session
# decides the current one is dead: re-resolving mid-playback lands on a
# different weaver node whose numbering does not continue the old one's.
Resolver = Callable[[], Awaitable[str]]

# Finds the same channel on a different player type, returning a candidate whose
# playlist is verified clean, or None when every type is carrying the ad. The
# bool is `full_quality_only`: set for the upgrade probe that runs behind an
# active low-quality bridge, which must not settle for a second degraded
# rendition. The last argument is the url being served, so the search can match
# its picture.
BackupFinder = Callable[
    [BackupState, str, bool, str | None], Awaitable[BackupCandidate | None]
]

# Builds the url of our own hold segment for a given sequence number. Injected
# the same way `rewrite_uri` is, so this module stays free of url shapes.
HoldUri = Callable[[int], str]

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


def _start_backup_search(
    session: StreamSession, backup: BackupFinder, *, full_quality_only: bool = False
) -> asyncio.Task | None:
    """Kick off one backup search in the background, if none is running.

    Whatever it finds lands in `session.spare`, which is the one place every
    consumer takes a candidate from - the start of a break, a hand-over when the
    active backup is stitched, a bridge upgrade. There used to be a separate
    pool, a prefetch result parked in the task handle and an upgrade result,
    each collected differently, and a candidate found early was routinely left
    unused while the break it was found for was held on black.
    """
    if _search_in_flight(session):
        return None

    async def search() -> BackupCandidate | None:
        try:
            # The served url tells the search which picture to match.
            candidate = await backup(
                session.backup, session.quality, full_quality_only, session.upstream_url
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed search is just no candidate
            log.warning("backup search failed", login=session.login, error=str(exc)[:200])
            candidate = None
        finally:
            session.backup.searching = False
        # Always published, "nothing usable" included: the pool judges every
        # token live, and a spare it no longer vouches for must not linger.
        session.spare = candidate
        return candidate

    session.backup.searching = True
    task = asyncio.create_task(search())
    session.backup_task = task
    _backup_tasks.add(task)
    task.add_done_callback(_backup_tasks.discard)
    return task


def _search_in_flight(session: StreamSession) -> bool:
    return session.backup_task is not None and not session.backup_task.done()


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
    # This poll was entirely advertising. Reported for diagnostics only - it no
    # longer means "nothing was served", because the window is always fed from
    # something (a backup if one was found, otherwise the ad itself).
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
    # Polls served from a backup stream instead of dead air.
    backup_polls: int = 0
    # Hold segments emitted: a break that no backup covered yet. Should rise
    # briefly at the start of a break and then stop, as the backup takes over.
    # Rising for the length of every break means no player type is coming back
    # clean on this channel.
    hold_segments: int = 0
    # Times an active low-quality bridge was upgraded to the session's quality.
    bridge_upgrades: int = 0
    # Times a stitched backup was replaced by the spare within the same poll -
    # a break covered without a single hold segment in the middle of it.
    backup_handovers: int = 0
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

    # Wall clock and duration of the last segment committed, so the next one can
    # be checked for a hole in *time*. The upstream index comparison alone cannot
    # see a gap that spans two polls, and that is exactly the kind that reaches a
    # player as a silent timestamp jump.
    last_pdt_epoch: float | None = None
    last_pdt_duration: float = 0.0

    # Length of the current run of ad-only polls, and whether the EXTINF-title
    # heuristic is still believed for this channel.
    consecutive_ad_polls: int = 0
    trust_titles: bool = True

    # Where the served timeline ends, as a PROGRAM-DATE-TIME epoch: the end of
    # the newest content segment committed, or of the newest ad segment covered
    # with holds. Every source - native, any backup, the hold - is folded in
    # against this, so a segment whose time slot is already on screen is never
    # served twice and a hand-over between sources neither repeats nor skips
    # time. Segment identity (`seen`) cannot do this on its own: an ad segment
    # and the content segment another token carries for the same moment have
    # different timestamps, and a break handed back and forth between sources
    # used to emit holds for time a backup had already covered - a burst of
    # tens of seconds of black at once, which pushed the player past its live
    # window and made it jump.
    timeline_end: float | None = None
    # What fed the last committed segment ("native", a backup url, or "hold"),
    # so any change of source is marked as a discontinuity however it happens.
    last_source: str | None = None

    # TTV-AB backup substitution. `in_break` spans the whole break, whatever is
    # covering it; `serving_backup` says the window is currently being fed from
    # another player type; `clean_native_polls` counts how long the real stream
    # has looked clean, so we do not switch back on the gap between two pods of
    # the same break.
    backup: BackupState = field(default_factory=BackupState)
    backup_task: asyncio.Task | None = field(default=None, repr=False)
    in_break: bool = False
    serving_backup: bool = False
    clean_native_polls: int = 0

    # A clean candidate held ready for the next time one is needed. See
    # SPARE_MAX_AGE.
    spare: BackupCandidate | None = None
    # Ad dateranges seen on the active backup, remembered for the same reason
    # the native ones are, but kept apart: a pod on one token says nothing about
    # what another token is carrying at that moment.
    backup_ad_ranges: dict[str, AdRange] = field(default_factory=dict)

    # Bridge bookkeeping. A backup accepted below the session's quality is held
    # for `BRIDGE_HOLD_SECONDS` while a full-quality candidate is probed behind
    # it; `bridge_upgrades` caps how often one break may pay for that seam.
    backup_promoted_at: float = 0.0
    bridge_upgrades: int = 0

    # Consecutive polls where the active backup could not be fetched. One is a
    # hiccup, covered from the native stream for that poll.
    backup_failures: int = 0
    # Polls the active backup has actually contributed content on. A backup that
    # has never delivered anything is not worth holding black for: it was
    # promoted on a "clean" verdict it no longer honours.
    backup_served_polls: int = 0

    # Sequence counter for our own hold segments. Only ever increases, so each
    # hold gets a distinct uri and the `seen` dedupe map cannot collapse a run
    # of them into one.
    hold_seq: int = 0
    # True while the window's newest segment is a hold, so the run only opens
    # one discontinuity instead of one per segment.
    holding: bool = False

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
            # Which player type is currently filling the break, if any, and how
            # long native has looked clean - the two numbers that say whether
            # backup substitution is working on this channel.
            "in_break": self.in_break,
            "serving_backup": self.serving_backup,
            "backup_player_type": self.backup.active.player_type if self.backup.active else None,
            "backup_quality": self.backup.active.quality if self.backup.active else None,
            "clean_native_polls": self.clean_native_polls,
            # Set while the break is being covered by a degraded rendition that
            # is still waiting on a full-quality candidate.
            "serving_bridge": bool(
                self.backup.active is not None and self.backup.active.is_bridge
            ),
            "bridge_upgrades": self.bridge_upgrades,
            "holding": self.holding,
            # The candidate held ready for the next hand-over. A break that
            # starts with none of these is the one that shows black.
            "spare_player_type": self.spare.player_type if self.spare else None,
            "spare_seasoned": self.spare.seasoned if self.spare else None,
            # Every warm backup token and what its last fetch said. A break with
            # none usable here is one that shows black.
            "warm_backups": [t.snapshot(now) for t in self.backup.warm.values()],
            # A search that is still in flight, and what the last attempt cost.
            # A rising `backup_last_attempt_s` is the tell for a slow player
            # type; `backup_searching` stuck true means a search is wedged.
            "backup_searching": self.backup.searching,
            "backup_last_attempt_s": self.backup.last_attempt_seconds,
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


def _remember_ad_ranges(session: StreamSession, parsed: ParsedPlaylist, now: float) -> bool:
    """Carry this poll's ad dateranges into the session's memory.

    Twitch stamps `#EXT-X-DATERANGE` once, at the head of a pod, and it scrolls
    out of the window long before the pod ends - so later polls of the same
    break carry only ad *segments*, recognisable only against the remembered
    range. This used to live in `_advance`, which meant a poll fed from anything
    other than the native stream forgot the break was happening: the very first
    ad poll is held rather than advanced, so the range was never recorded at all
    and the second poll of every break read as clean content.

    Returns True when this poll announced a break not seen before - the
    earliest sign of one, often a poll ahead of the first ad segment.
    """
    new_ad_ranges = False
    for rng in parsed.ad_ranges:
        if rng.id not in session.ad_ranges:
            new_ad_ranges = True
        session.ad_ranges.setdefault(rng.id, rng).first_seen = now
    _prune_ad_ranges(session, now)
    return new_ad_ranges


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


def _covered(session: StreamSession, seg: UpstreamSegment) -> bool:
    """Is this segment's time slot already on the served timeline?

    Judged on the midpoint so two sources whose segment boundaries do not line
    up exactly still hand over cleanly: a segment mostly in the past is
    skipped, one mostly in the future is served.
    """
    if session.timeline_end is None or seg.program_date_epoch is None:
        return False
    return seg.program_date_epoch + seg.duration / 2 <= session.timeline_end


def _extend_timeline(session: StreamSession, seg: UpstreamSegment, *, ad: bool) -> None:
    """Move the served timeline's end past a segment just committed or held.

    An ad's own timestamp is only trusted to move the end forward by the time
    actually held for it. Content is authoritative about where the timeline
    is; an ad creative's clock is Twitch's to get wrong, and a timeline end
    pushed ahead of reality would have every real segment after the break
    skipped as already covered - a frozen picture instead of a black one.
    """
    if seg.program_date_epoch is None:
        return
    end = seg.program_date_epoch + seg.duration
    if ad and session.timeline_end is not None:
        end = min(end, session.timeline_end + seg.duration + PDT_GAP_TOLERANCE)
    if session.timeline_end is None or end > session.timeline_end:
        session.timeline_end = end


def _check_clock(session: StreamSession, parsed: ParsedPlaylist) -> None:
    """Re-base the timeline on a source whose clock plainly disagrees with it.

    See CLOCK_RESET_SECONDS. Without this, a source stamped far behind the
    timeline would never contribute a segment at all.
    """
    if session.timeline_end is None:
        return
    tail = next((s for s in reversed(parsed.segments) if s.program_date_epoch is not None), None)
    if tail is None:
        return
    behind = session.timeline_end - (tail.program_date_epoch + tail.duration)
    if behind > CLOCK_RESET_SECONDS:
        log.warning(
            "source clock is far behind the served timeline; re-basing on it",
            login=session.login,
            seconds=round(behind, 1),
        )
        session.timeline_end = None


def _has_uncovered_ads(session: StreamSession, parsed: ParsedPlaylist) -> bool:
    """Does this source have advertising for time we have not yet served?

    The one question a break turns on. An ad the timeline has already moved
    past - covered by a backup, or by holds - needs nothing more; an ad beyond
    it is a gap that something has to fill before the player reaches it.
    """
    return any(
        seg.is_ad
        and not _covered(session, seg)
        and segment_key(seg, session.generation) not in session.seen
        for seg in parsed.segments
    )


def _advance(
    session: StreamSession,
    parsed: ParsedPlaylist,
    *,
    strip_ads: bool,
    rewrite_uri: UriRewriter | None,
    now: float,
    hold_uri: HoldUri | None = None,
    source: str = "native",
) -> int:
    """Fold one poll into the session. Returns the number of segments removed.

    With `hold_uri`, a stripped ad segment is not dropped but *replaced*, the
    moment it first appears, by the same length of hold segments. Dropping it
    was the freeze at the start of every break: a break begins at the live edge
    while real content is still in the window, so the pod was cut out, nothing
    new was appended, and the playlist stood still for the ~30s it took the ads
    to fill the whole window - by which time the player had long run dry. The
    replacement keeps the timeline growing at exactly real time, however the
    break lines up with the window.
    """
    rewriter = rewrite_uri or (lambda u: u)

    # Target duration must never shrink: a mid-session drop makes players
    # re-evaluate their buffer and stall.
    session.target_duration = max(session.target_duration, parsed.target_duration)
    if parsed.version:
        session.version = max(session.version, parsed.version)
    if parsed.passthrough_tags:
        session.passthrough_tags = list(parsed.passthrough_tags)

    substitute = strip_ads and hold_uri is not None
    if strip_ads and not substitute:
        kept = [s for s in parsed.segments if not s.is_ad]
    else:
        kept = list(parsed.segments)
    removed = sum(1 for s in parsed.segments if s.is_ad) if strip_ads else 0

    _check_clock(session, parsed)
    prev_index: int | None = None
    for seg in kept:
        key = segment_key(seg, session.generation)
        if key in session.seen or _covered(session, seg):
            # Already on the timeline - this segment, or another source's
            # segment for the same moment.
            prev_index = seg.index
            session.last_pdt_epoch = seg.program_date_epoch
            session.last_pdt_duration = seg.duration
            continue
        if substitute and seg.is_ad:
            _append_hold_for(session, hold_uri, seg.duration, now)
            session.seen[key] = (session.next_seq - 1, now + SEEN_TTL)
            _extend_timeline(session, seg, ad=True)
            prev_index = seg.index
            # Continuity is measured across the break: the content after it
            # carries on from where the ad's own timestamps end.
            session.last_pdt_epoch = seg.program_date_epoch
            session.last_pdt_duration = seg.duration
            continue

        # A hole in the upstream indices means segments were removed here.
        gap = prev_index is not None and seg.index != prev_index + 1
        # Leaving a run of holds is a source change.
        _end_hold(session)
        discontinuity = (
            seg.discontinuity_before
            or gap
            or session.pending_discontinuity
            or (session.last_source is not None and source != session.last_source)
            or _pdt_gap(session, seg)
        )
        session.pending_discontinuity = False
        session.last_source = source

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
        _extend_timeline(session, seg, ad=False)
        prev_index = seg.index
        session.last_pdt_epoch = seg.program_date_epoch
        session.last_pdt_duration = seg.duration

    _evict(session)
    _prune_seen(session, now)
    return removed


def _pdt_gap(session: StreamSession, seg: UpstreamSegment) -> bool:
    """Did wall-clock time move further than the media timeline did?

    The index comparison above only sees holes *within* one poll. A hole that
    spans two polls - a skipped poll, an upstream jump, a pod that consumed a
    whole window - leaves the indices looking contiguous, so the output would
    carry a timestamp jump with nothing marking it. Comparing
    `#EXT-X-PROGRAM-DATE-TIME` against the duration we actually served catches
    those, which matters because an unmarked jump is the one thing a player
    cannot compensate for.
    """
    if seg.program_date_epoch is None or session.last_pdt_epoch is None:
        return False
    expected = session.last_pdt_epoch + session.last_pdt_duration
    drift = seg.program_date_epoch - expected
    if drift <= PDT_GAP_TOLERANCE:
        return False
    log.info(
        "timeline gap detected from program-date-time",
        login=session.login,
        seconds=round(drift, 3),
    )
    return True


HOLD_SEGMENT_SECONDS = 1.0


def _append_hold(session: StreamSession, hold_uri: HoldUri, now: float) -> None:
    """Commit one hold segment: our own black, silent, decodable second.

    This is TTV-AB's `_createEmptyAdHoldPlaylist`, and it exists because the two
    obvious alternatives are both worse. Serving the ad shows the ad. Serving
    nothing leaves the playlist static, and a live playlist that stops advancing
    makes ffmpeg declare the stream over - and whatever content the break hid
    comes back as an uncompensated timestamp jump, because ffmpeg's HLS demuxer
    ignores `#EXT-X-DISCONTINUITY` (trac #5419).

    A hold keeps the media timeline moving at real time with something a decoder
    can actually decode, so there is never a hole to compensate for. The picture
    waits; it does not drift.

    Each hold gets a fresh `hold_seq` so its uri is unique - the `seen` map is
    keyed by uri, and a repeated one would be recognised as already-served and
    silently dropped, which is exactly the static playlist this avoids.
    """
    session.hold_seq += 1
    key = f"hold:{session.generation}:{session.hold_seq}"
    out = OutputSegment(
        seq=session.next_seq,
        uri=hold_uri(session.hold_seq),
        duration=HOLD_SEGMENT_SECONDS,
        key=key,
        title="live",
        # Only the first hold of a run opens a discontinuity. The rest are the
        # same source as the one before, and a marker per segment would inflate
        # DISCONTINUITY-SEQUENCE into nonsense as they scroll off.
        discontinuity=not session.holding or session.pending_discontinuity,
    )
    session.pending_discontinuity = False
    session.holding = True
    session.last_source = "hold"
    session.next_seq += 1
    session.window.append(out)
    session.seen[key] = (out.seq, now + SEEN_TTL)
    session.stats.hold_segments += 1
    # The hold is not upstream content, so the PDT continuity check must not
    # measure the next real segment against it.
    session.last_pdt_epoch = None
    session.last_pdt_duration = 0.0
    _evict(session)
    _prune_seen(session, now)


def _append_hold_for(
    session: StreamSession, hold_uri: HoldUri, seconds: float, now: float
) -> None:
    """Cover `seconds` of removed advertising with one-second holds.

    The count is rounded, not floored: a 2.002s ad segment is two holds, so the
    output timeline tracks real time instead of falling a little further behind
    with every segment of a long break.
    """
    for _ in range(max(1, round(seconds / HOLD_SEGMENT_SECONDS))):
        _append_hold(session, hold_uri, now)


def _end_hold(session: StreamSession) -> None:
    """Leave a hold run, marking the seam to whatever feeds the window next."""
    if session.holding:
        session.holding = False
        session.pending_discontinuity = True


async def _take_spare(
    session: StreamSession,
    fetch: Fetcher,
    now: float,
    *,
    full_quality_only: bool = False,
) -> tuple[BackupCandidate, str] | None:
    """Claim the spare, re-checked against its playlist *now*.

    Returns the candidate and the playlist that proved it usable, so the caller
    can serve from that fetch instead of making another. The check is the same
    one a serving backup is held to: its live edge is content, and it has no
    advertising for time the session has not served yet. An ad further back in
    its window - the preroll a warm token played out before the break - is in
    time already on screen, and costs nothing.
    """
    candidate = session.spare
    if candidate is None:
        return None
    if full_quality_only and candidate.is_bridge:
        return None
    session.spare = None
    status, playlist = await fetch(candidate.url)
    reason = None
    if status != 200 or not playlist:
        reason = f"status-{status}"
    else:
        parsed = hls.parse_media_playlist(
            playlist, candidate.url, strip_ads=True, now=now, trust_titles=session.trust_titles
        )
        if not parsed.segments:
            reason = "not-playable"
        elif parsed.ends_in_ad or _has_uncovered_ads(session, parsed):
            reason = "ad-marked"
    if reason is not None:
        log.info(
            "discarding backup candidate (no longer clean)",
            login=session.login,
            player_type=candidate.player_type,
            age=round(candidate.age(now), 1),
            reason=reason,
        )
        return None
    return candidate, playlist


def _promote(session: StreamSession, candidate: BackupCandidate, now: float) -> None:
    """Make a verified candidate the source the window is fed from."""
    session.backup.active = candidate
    session.serving_backup = True
    session.backup_promoted_at = now
    session.backup_failures = 0
    session.backup_served_polls = 0
    session.backup_ad_ranges = {}
    session.pending_discontinuity = True


def _maintain_spare(session: StreamSession, backup: BackupFinder) -> None:
    """Refresh the warm pool, once per poll.

    The refresh is what keeps every backup token's playlist being fetched, so
    each plays out its preroll long before a break needs it - and what keeps
    the spare an up-to-the-poll verdict rather than a stale one.
    """
    _start_backup_search(session, backup)


async def _maybe_upgrade_bridge(
    session: StreamSession,
    fetch: Fetcher,
    now: float,
) -> None:
    """Trade up from a low-quality backup once a full-quality one is usable.

    A break covered at 360p is better than black, but not what anyone wants for
    a whole midroll. The warm pool keeps judging the full-quality tokens while
    it plays, and the moment one has come out of its own ad it is re-checked
    and swapped in - keeping the 360p picture if that check fails.

    Held for BRIDGE_HOLD_SECONDS first, and capped at MAX_BRIDGE_UPGRADES per
    break: each swap is a seam and a resolution change.
    """
    active = session.backup.active
    if active is None or not active.is_bridge:
        return
    if session.bridge_upgrades >= MAX_BRIDGE_UPGRADES:
        return
    if now - session.backup_promoted_at < BRIDGE_HOLD_SECONDS:
        return
    spare = session.spare
    if spare is None or spare.is_bridge or not spare.seasoned:
        return
    session.bridge_upgrades += 1
    taken = await _take_spare(session, fetch, now, full_quality_only=True)
    if taken is None:
        return
    candidate = taken[0]
    log.info(
        "upgrading from the low-quality backup",
        login=session.login,
        player_type=candidate.player_type,
        quality=candidate.quality,
        attempt=session.bridge_upgrades,
    )
    _promote(session, candidate, now)
    session.stats.bridge_upgrades += 1


async def _apply_backup(
    session: StreamSession,
    backup: BackupFinder,
    *,
    needs_cover: bool,
    native_ad_edge: bool,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None,
    now: float,
    hold_uri: HoldUri | None = None,
) -> bool:
    """Cover an ad break, and never let the ad through.

    Returns True when the window was fed here this poll, so the caller knows not
    to fold the native poll as well. Returning False during a break does *not*
    mean the ad gets served: the caller holds instead. See `get_playlist`.

    A break is covered from its *first* ad segment. It used to wait until every
    segment in the native window was advertising, which on Twitch is 30s or
    more after the break began - all of it held on black while a clean
    candidate sat unused, until that candidate had itself been stitched and was
    thrown away. `needs_cover` - native has advertising for time not yet
    served - is now the trigger, and the spare is what makes acting on it
    instant.
    """
    native_clean = not needs_cover and not native_ad_edge
    if session.in_break:
        if native_clean:
            # Wait for native to stay clean before switching back: one clean
            # poll is routinely the gap between two pods of one break.
            session.clean_native_polls += 1
            if session.clean_native_polls >= MIN_CLEAN_POLLS_TO_RESUME:
                _end_break(session)
                return False
        else:
            session.clean_native_polls = 0
    elif needs_cover:
        session.in_break = True
        session.clean_native_polls = 0
    else:
        return False

    if session.serving_backup:
        await _maybe_upgrade_bridge(session, fetch, now)
        return await _serve_backup(session, backup, fetch, rewrite_uri, now, hold_uri)

    if not needs_cover:
        # Mid-break, nothing new to cover this poll: native (with any ads
        # already held over) is what feeds the window.
        return False
    taken = await _take_spare(session, fetch, now)
    if taken is None:
        # Nothing ready. Hold this poll and have a candidate by the next one.
        _start_backup_search(session, backup)
        return False
    candidate, playlist = taken
    _promote(session, candidate, now)
    log.info(
        "splicing backup stream over the ad break",
        login=session.login,
        player_type=candidate.player_type,
        quality=candidate.quality,
        bridge=candidate.is_bridge,
        seasoned=candidate.seasoned,
    )
    return await _serve_backup(
        session, backup, fetch, rewrite_uri, now, hold_uri, prefetched=playlist
    )


async def _serve_backup(
    session: StreamSession,
    backup: BackupFinder,
    fetch: Fetcher,
    rewrite_uri: UriRewriter | None,
    now: float,
    hold_uri: HoldUri | None = None,
    *,
    prefetched: str | None = None,
) -> bool:
    """Poll the active backup and fold its newest segments in.

    Returns True when the window was fed from here.

    The moment the backup has advertising of its own for time not yet served,
    it is handed over to the spare *within this poll*. It used to be held
    through up to four fully-advertising polls first - each of them emitting
    black for the backup's ad segments - and only then dropped, with nothing
    ready to replace it: ten seconds or more of black per hand-over, several
    times in a long break. Twitch does not un-stitch a pod from a token, so
    there was never anything to wait for; a *fresh* token is what comes back
    clean.
    """
    for _ in range(2):
        active = session.backup.active
        if active is None:
            return False
        if prefetched is not None:
            status, playlist = 200, prefetched
            prefetched = None
        else:
            status, playlist = await fetch(active.url)

        parsed = None
        if status == 200 and playlist:
            parsed = hls.parse_media_playlist(
                playlist,
                active.url,
                strip_ads=True,
                known_ad_ranges=session.backup_ad_ranges,
                now=now,
                trust_titles=session.trust_titles,
            )
        if parsed is None or not parsed.segments:
            session.backup_failures += 1
            if session.backup_failures < MAX_BACKUP_FAILURES:
                # A hiccup: the caller covers this poll from native.
                return False
            _drop_backup(session, active, "error" if parsed is None else "not-playable", now)
        else:
            session.backup_failures = 0
            for rng in parsed.ad_ranges:
                session.backup_ad_ranges.setdefault(rng.id, rng)
            if not parsed.ends_in_ad and not _has_uncovered_ads(session, parsed):
                _advance(
                    session,
                    parsed,
                    strip_ads=True,
                    rewrite_uri=rewrite_uri,
                    now=now,
                    hold_uri=hold_uri,
                    source=active.url,
                )
                session.backup_served_polls += 1
                session.stats.backup_polls += 1
                return True
            log.info(
                "backup stream is carrying the ad too; handing over",
                login=session.login,
                player_type=active.player_type,
                served_polls=session.backup_served_polls,
                served_seconds=round(now - session.backup_promoted_at, 1),
                spare=session.spare.player_type if session.spare else None,
            )
            _drop_backup(session, active, "ad-marked", now)

        taken = await _take_spare(session, fetch, now)
        if taken is None:
            # Nothing ready: the caller holds for this poll's uncovered time,
            # and the search has a candidate for the next one.
            _start_backup_search(session, backup)
            return False
        candidate, prefetched = taken
        _promote(session, candidate, now)
        session.stats.backup_handovers += 1
        log.info(
            "handing the ad break over to a fresh backup stream",
            login=session.login,
            player_type=candidate.player_type,
            quality=candidate.quality,
            bridge=candidate.is_bridge,
        )
    return False


def _drop_backup(
    session: StreamSession, active: BackupCandidate, reason: str, now: float
) -> None:
    """Give up on the active backup.

    Nothing is cooled down or excluded: the warm pool keeps judging this token
    along with the others, and offers it again the moment its live edge is
    content - which, for a token that has just been stitched, is when its pod
    has played out.
    """
    session.backup.clear()
    session.serving_backup = False
    session.backup_failures = 0
    session.backup_served_polls = 0


def _end_break(session: StreamSession) -> None:
    """Native has stayed clean: the break is over."""
    if session.serving_backup:
        log.info(
            "ad break over; returning to the native stream",
            login=session.login,
            clean_polls=session.clean_native_polls,
            backup=session.backup.active.player_type if session.backup.active else None,
            handovers=session.stats.backup_handovers,
        )
        session.pending_discontinuity = True
    session.in_break = False
    session.serving_backup = False
    session.clean_native_polls = 0
    session.backup.clear()
    session.bridge_upgrades = 0
    session.backup_failures = 0
    session.backup_served_polls = 0
    session.backup_ad_ranges = {}


def _render(session: StreamSession) -> PlaylistRender:
    """Render the current window.

    There is no longer a segment-less variant. Serving headers only was how an
    ad break used to be handled, and it cost the stream twice over: the player
    starved for the length of the pod, and the content we skipped came back as a
    timestamp jump that ffmpeg's HLS demuxer does not compensate for, leaving
    audio behind by the length of the break. The window is always fed from
    something now - native, a backup, or the ad itself - so this always has
    segments to render.
    """
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
    url = await resolve()
    if force and session.upstream_url and url != session.upstream_url:
        session.generation += 1
        # A different weaver node is a different encoder instance, and nothing
        # promises its timestamps continue the old one's. PDT-based identity
        # makes the switch invisible to dedupe, which is what we want, but the
        # seam still has to be declared or a player has no idea the timeline
        # moved under it.
        session.pending_discontinuity = True
        log.info("upstream weaver changed; marking the seam", login=session.login)
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
    hold_uri: HoldUri | None = None,
) -> PlaylistRender:
    """Return the client-facing playlist for this channel, advancing the session.

    One source feeds the window per poll, chosen in this order:

    1. the native stream, when it is clean;
    2. a clean backup on another player type, when one has been found;
    3. our own hold segment.

    The ad is never folded in. Cutting a pod out and serving *nothing* is what
    is forbidden: that leaves a hole the length of the break, and ffmpeg's HLS
    demuxer ignores `#EXT-X-DISCONTINUITY` (trac #5419), so the hole reaches
    Jellyfin as an uncompensated timestamp jump and desyncs audio by the length
    of what was removed, cumulatively across a session. A hold segment keeps the
    timeline advancing at real time with decodable media in it, so there is no
    hole to compensate for - the picture waits instead of drifting.

    `backup` enables TTV-AB-style substitution and `hold_uri` supplies the hold
    segment's url; both are None only on paths with no ad handling at all, and
    without them a break falls back to passing the ad through.
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

        # Decided before anything is folded, because it is what selects the
        # source.
        _remember_ad_ranges(session, parsed, now)
        # The whole window is advertising. Diagnostics and the title-heuristic
        # guard only - it no longer selects anything: by the time it is true
        # the break has been under way for a full window.
        ad_pod = bool(
            strip_ads and parsed.segments and all(s.is_ad for s in parsed.segments)
        )
        # The live edge is advertising: a break is running.
        ad_incoming = bool(strip_ads and parsed.ends_in_ad)
        # Native has advertising for time not yet served. This is what a
        # backup is spliced in for, from the first ad segment of a break.
        needs_cover = bool(strip_ads and _has_uncovered_ads(session, parsed))
        # The ad-only run is counted here, not in `_advance`, because it is a
        # property of the *native* stream: passing a pod through commits ad
        # segments as ordinary content, and a backup splice commits somebody
        # else's content, so neither says anything about whether the break ended.
        if ad_pod:
            session.consecutive_ad_polls += 1
            session.stats.ad_pod_polls += 1
            _distrust_titles_if_endless(session, parsed)
        else:
            session.consecutive_ad_polls = 0

        served_backup = False
        if backup is not None:
            served_backup = await _apply_backup(
                session,
                backup,
                needs_cover=needs_cover,
                native_ad_edge=ad_incoming,
                fetch=fetch,
                rewrite_uri=rewrite_uri,
                now=now,
                hold_uri=hold_uri,
            )
            _maintain_spare(session, backup)
        if served_backup:
            _end_hold(session)

        # A break the backup could not cover is held, not shown: every ad
        # segment is replaced by holds of the same length as it arrives (see
        # `_advance`). This is independent of `backup` so it still applies while
        # a search is in flight, when every player type is dirty, and on a
        # deployment with no backup search at all.
        removed = 0
        held = False
        if not served_backup and strip_ads and hold_uri is not None:
            removed = _advance(
                session,
                parsed,
                strip_ads=True,
                rewrite_uri=rewrite_uri,
                now=now,
                hold_uri=hold_uri,
            )
            held = True

        if not served_backup and not held:
            _end_hold(session)
            # `strip_ads and not ad_pod` is the whole policy: strip ads out of a
            # window that still has real content either side of them, but never
            # cut a hole in the timeline.
            #
            # Reaching here with `ad_pod` set means there is no `hold_uri` on
            # this path, so there is nothing to substitute and the ad is passed
            # through to keep the timeline continuous. With one wired up, the
            # break is held above and this branch is not reached.
            if ad_pod:
                log.info(
                    "ad pod with no ad handling on this path - passing it "
                    "through to keep the timeline continuous",
                    login=session.login,
                    segments=len(parsed.segments),
                    consecutive=session.consecutive_ad_polls,
                )
            removed = _advance(
                session,
                parsed,
                strip_ads=strip_ads and not ad_pod,
                rewrite_uri=rewrite_uri,
                now=now,
            )
        session.stats.removed_segments += removed

        render = _render(session)
        render.ad_pod = ad_pod
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


def touch_any(login: str) -> None:
    """Keep every session for this channel alive.

    The segment endpoint knows the login but not the quality, and looking the
    quality up meant a database round-trip on the hottest path in the app - once
    per segment, per viewer. A channel has one or two sessions at most, so
    touching all of them is both cheaper and immune to getting the key wrong,
    which is how a session with a per-channel quality override used to be swept
    out from under a live client.
    """
    now = time.monotonic()
    for session in _sessions.values():
        if session.login == login:
            session.last_access = now


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

    # Mirrors the source choice `get_playlist` makes, minus the backup search
    # and the hold: an all-ad poll is passed through here so the caller can see
    # exactly what upstream sent.
    _remember_ad_ranges(session, parsed, now)
    ad_pod = bool(strip_ads and parsed.segments and all(s.is_ad for s in parsed.segments))
    removed = _advance(
        session, parsed, strip_ads=strip_ads and not ad_pod, rewrite_uri=rewrite_uri, now=now
    )
    render = _render(session)

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
    clone.timeline_end = session.timeline_end
    clone.last_source = session.last_source
    clone.window = deque(copy.deepcopy(list(session.window)))
    clone.seen = OrderedDict(session.seen)
    clone.ad_ranges = copy.deepcopy(session.ad_ranges)
    clone.consecutive_ad_polls = session.consecutive_ad_polls
    clone.trust_titles = session.trust_titles
    clone.target_duration = session.target_duration
    clone.version = session.version
    clone.passthrough_tags = list(session.passthrough_tags)
    clone.last_rendered = session.last_rendered
    clone.last_media_sequence = session.last_media_sequence
    clone.last_upstream_tail = session.last_upstream_tail
    clone.last_change_at = session.last_change_at
    clone.is_master = session.is_master
    return clone
