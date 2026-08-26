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

# Longest we will freeze the picture rather than emit a segment we believe is an
# ad. Comfortably past any real Twitch midroll, so reaching it means the
# classification is wrong rather than the break being long. Bounding this is
# what makes aggressive detection safe: a false positive costs a stall, not a
# dead channel.
MAX_AD_HOLD_SECONDS = 240.0

# Jellyfin's probe and its ffmpeg both hit the playlist URL; without a short
# render cache they advance the window twice per real poll.
RENDER_CACHE_SECONDS = 1.0

MIN_RESOLVE_INTERVAL = 5.0
MAX_CONSECUTIVE_FAILURES = 3

SESSION_IDLE_SECONDS = 90.0
SESSION_MAX_SECONDS = 6 * 60 * 60

# Upstream statuses that mean "this weaver URL is dead, get a new one".
_DEAD_STATUSES = frozenset({0, 400, 403, 404, 410})

Resolver = Callable[[], Awaitable[str]]
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
    ad_fallback: bool = False
    from_cache: bool = False


@dataclass
class SessionStats:
    polls: int = 0
    resolves: int = 0
    ad_fallback_polls: int = 0
    # Polls where the whole upstream window was ads and we emitted nothing.
    # A healthy channel shows these rising during a break and then stopping;
    # ad_fallback_polls rising instead means ads reached the player.
    ad_hold_polls: int = 0
    # Times a hold outlasted MAX_AD_HOLD_SECONDS and had to be broken. Should be
    # zero on a healthy channel; anything else means detection misfired.
    ad_hold_giveups: int = 0
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

    # When the current run of ad-only polls began (0.0 = not holding), and
    # whether the EXTINF-title heuristic is still believed for this channel.
    hold_started_at: float = 0.0
    trust_titles: bool = True

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
            # `trust_titles: false` means the EXTINF-title heuristic misfired on
            # this channel and was revoked - the first place to look if a stream
            # stalled once and then played normally.
            "trust_titles": self.trust_titles,
            "holding_s": round(now - self.hold_started_at, 1) if self.hold_started_at else 0.0,
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


def _hold_exhausted(session: StreamSession, parsed: ParsedPlaylist, now: float) -> bool:
    """Has this hold outlasted any believable ad break? If so, fix the cause.

    Holding output is right for the length of a real pod and catastrophic past
    it: a misclassification would otherwise freeze the channel for as long as
    the viewer is willing to stare at it. The remedy depends on what put us
    here, so this decides that too rather than blindly unsticking.
    """
    if not session.hold_started_at:
        return False
    if now - session.hold_started_at <= MAX_AD_HOLD_SECONDS:
        return False

    session.hold_started_at = 0.0
    session.stats.ad_hold_giveups += 1

    title_only = all(s.ad_source == "title" for s in parsed.segments if s.is_ad)
    if title_only and not session.ad_ranges:
        # The EXTINF-title heuristic matched every segment for minutes on end
        # with no ad daterange ever corroborating it. A real pod ends; a stream
        # title does not - this is a channel whose *name* trips the pattern
        # (the "Amazon haul unboxing" case). Revoke the heuristic for this
        # session so it cannot re-trigger the moment we resume.
        session.trust_titles = False
        log.warning(
            "ad title heuristic held output too long with no corroborating "
            "daterange; disabling it for this session",
            login=session.login,
            held_s=round(MAX_AD_HOLD_SECONDS, 1),
        )
    else:
        # Believable markers, implausible duration - the remembered ranges have
        # probably gone stale. Drop them and get a fresh upstream url, which
        # normally lands past the pod.
        session.ad_ranges.clear()
        session.upstream_url = None
        log.warning(
            "ad hold exceeded the maximum; dropping ad ranges and re-resolving",
            login=session.login,
            held_s=round(MAX_AD_HOLD_SECONDS, 1),
        )
    return True


def _advance(
    session: StreamSession,
    parsed: ParsedPlaylist,
    *,
    strip_ads: bool,
    rewrite_uri: UriRewriter | None,
    now: float,
) -> tuple[int, bool]:
    """Fold one poll into the session. Returns (removed_segments, ad_fallback)."""
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

    ad_fallback = False
    if strip_ads and parsed.segments and not kept:
        if session.window and not _hold_exhausted(session, parsed, now):
            # Every segment upstream is an ad. Emitting nothing is the whole
            # point: the window still holds real content, so the rendered
            # playlist stays valid and non-empty, and the player simply waits at
            # the live edge until the pod ends. This is what streamlink does too
            # - it pauses output for the duration of the ad and resumes with a
            # discontinuity. Passing the pod through instead is what produced
            # full-quality ad breaks, which is the one outcome ad stripping
            # exists to prevent.
            if not session.hold_started_at:
                session.hold_started_at = now
            session.pending_discontinuity = True
            session.stats.ad_hold_polls += 1
            log.info(
                "holding output through an ad pod",
                login=session.login,
                segments=len(parsed.segments),
                held_s=round(now - session.hold_started_at, 1),
            )
        elif session.window:
            # The hold ran past MAX_AD_HOLD_SECONDS - longer than any real
            # midroll - so the classification is not believable any more.
            # `_hold_exhausted` has already applied the remedy; serve this poll
            # so playback is not left frozen while it takes effect.
            kept = list(parsed.segments)
            removed = 0
            ad_fallback = True
        else:
            # Cold session: there is no window to fall back on, and an empty
            # playlist is fatal to ffmpeg. Serving the pod is the lesser evil
            # only here, at the very start of a session.
            kept = list(parsed.segments)
            removed = 0
            ad_fallback = True
            log.warning(
                "ad pod on a cold session; passing segments through to avoid an "
                "empty playlist",
                login=session.login,
                segments=len(parsed.segments),
            )

    prev_index: int | None = None
    for seg in kept:
        key = segment_key(seg, session.generation)
        if key in session.seen:
            prev_index = seg.index
            continue

        # Real output resumed, so whatever hold was running is over. Measuring
        # the hold from the last *emitted* segment rather than the last poll is
        # what stops a long break from being read as a stuck one.
        session.hold_started_at = 0.0

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
    return removed, ad_fallback


def _render(session: StreamSession) -> PlaylistRender:
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
    session.upstream_url = url
    return url


async def _poll_upstream(
    session: StreamSession,
    *,
    resolve: Resolver,
    fetch: Fetcher,
    strip_ads: bool,
    now: float,
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
) -> PlaylistRender:
    """Return the client-facing playlist for this channel, advancing the session."""
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
                session, resolve=resolve, fetch=fetch, strip_ads=strip_ads, now=now
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
        removed, ad_fallback = _advance(
            session, parsed, strip_ads=strip_ads, rewrite_uri=rewrite_uri, now=now
        )
        session.stats.removed_segments += removed
        if ad_fallback:
            session.stats.ad_fallback_polls += 1

        render = _render(session)
        render.removed_segments = removed
        render.ad_fallback = ad_fallback
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


def drop(login: str, quality: str, variant: str | None = None) -> None:
    _sessions.pop(session_key(login, quality, variant), None)


def reset() -> None:
    """Drop every session. Used by tests and by settings changes."""
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
        _sessions.pop(key, None)
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

    removed, ad_fallback = _advance(
        session, parsed, strip_ads=strip_ads, rewrite_uri=rewrite_uri, now=now
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
            "ad_fallback": ad_fallback,
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
    clone.hold_started_at = session.hold_started_at
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
