"""HLS playlist parsing and rendering, including Twitch stitched-ad removal.

Twitch splices ads directly into the live media playlist. The ad window is
announced with an `#EXT-X-DATERANGE` tag; the segments that follow it are ad
content. streamlink identifies those dateranges with this predicate:

    CLASS == "twitch-stitched-ad"
    or ID starts with "stitched-ad-"
    or any attribute key starts with "X-TV-TWITCH-AD-"

This module is deliberately split into `parse_media_playlist` (text -> structured
segments, ads *marked* rather than dropped) and `render_media_playlist`
(structured segments -> text). Keeping the two apart is what lets
`services.stream_session` hold state across polls: it needs to see every segment,
including the ads, to decide which ones it has already handed out and what
sequence number each one was given. A single-pass "transform the text" function
cannot express that, and the version that tried is what produced non-monotonic
`#EXT-X-MEDIA-SEQUENCE` output.

Ad detection is a heuristic against an undocumented, changing format, so it lives
in one small module on purpose: cheap to test, cheap to fix.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')

AD_CLASS = "twitch-stitched-ad"
AD_ID_PREFIX = "stitched-ad-"
AD_ATTR_PREFIX = "X-TV-TWITCH-AD-"

# Dateranges that carry X-TV-TWITCH-* attributes but are not ad markers. Without
# this, the "any X-TV-TWITCH-AD- attribute" rule over-matches and blanks the
# stream.
_NON_AD_CLASSES = {"twitch-trigger", "twitch-stream-source", "twitch-info"}

# Last-resort hint: Twitch sometimes labels ad segments in the EXTINF title.
# Note the absence of a bare `amazon` alternative - it used to be there, and it
# meant a stream titled "Amazon haul" lost every single segment.
_AD_TITLE_RE = re.compile(r"(stitched-ad|twitch-ad|amazon\s+ad)", re.IGNORECASE)

# How much content a daterange that announces *no* duration is allowed to eat.
# This was 240s, which is longer than the entire sliding window - a single
# duration-less daterange therefore stripped the playlist down to nothing, and an
# empty playlist is a fatal error for ffmpeg.
_UNKNOWN_AD_WINDOW_SECONDS = 60.0

# Low-latency / delta-update tags we deliberately drop. We serve a plain HLS
# playlist, and these carry URIs relative to an upstream host the client cannot
# resolve, plus state (EXT-X-SKIP) we never negotiate.
_DROPPED_TAG_PREFIXES = (
    "#EXT-X-PART:",
    "#EXT-X-PART-INF:",
    "#EXT-X-PRELOAD-HINT:",
    "#EXT-X-RENDITION-REPORT:",
    "#EXT-X-SERVER-CONTROL:",
    "#EXT-X-SKIP:",
)

UriRewriter = Callable[[str], str]


def parse_attributes(value: str) -> dict[str, str]:
    """Parse an HLS tag attribute list into a dict (quotes stripped)."""
    attrs: dict[str, str] = {}
    for key, raw in _ATTR_RE.findall(value):
        attrs[key.upper()] = raw.strip('"')
    return attrs


def is_ad_daterange(attrs: dict[str, str]) -> bool:
    cls = attrs.get("CLASS", "").lower()
    if cls == AD_CLASS:
        return True
    if attrs.get("ID", "").startswith(AD_ID_PREFIX):
        return True
    if cls in _NON_AD_CLASSES:
        return False
    return any(key.startswith(AD_ATTR_PREFIX) for key in attrs)


def _float_attr(attrs: Mapping[str, str], *keys: str) -> float | None:
    for key in keys:
        raw = attrs.get(key)
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return None


def _extinf_duration(line: str) -> float:
    body = line.split(":", 1)[1] if ":" in line else ""
    head = body.split(",", 1)[0].strip()
    try:
        return float(head)
    except ValueError:
        return 0.0


def _extinf_title(line: str) -> str:
    body = line.split(":", 1)[1] if ":" in line else ""
    return body.split(",", 1)[1].strip() if "," in body else ""


def parse_iso8601(value: str | None) -> float | None:
    """ISO-8601 timestamp -> epoch seconds. Tolerates `Z` and missing fractions."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------- models
@dataclass(slots=True)
class AdRange:
    """A stitched-ad `#EXT-X-DATERANGE`, remembered across polls."""

    id: str
    start_epoch: float | None = None
    duration: float | None = None
    first_seen: float = 0.0

    def covers(self, epoch: float) -> bool:
        """Does this ad window contain the given segment timestamp?"""
        if self.start_epoch is None:
            return False
        span = self.duration if self.duration is not None else _UNKNOWN_AD_WINDOW_SECONDS
        return self.start_epoch <= epoch < self.start_epoch + span


@dataclass(slots=True)
class UpstreamSegment:
    """One segment as it appeared in the upstream playlist."""

    uri: str
    duration: float
    index: int
    upstream_seq: int
    title: str = ""
    program_date_time: str | None = None
    program_date_epoch: float | None = None
    byterange: str | None = None
    discontinuity_before: bool = False
    is_ad: bool = False
    ad_source: str | None = None  # "daterange" | "title" | "session-range"


@dataclass(slots=True)
class ParsedPlaylist:
    version: int | None = None
    target_duration: float = 2.0
    media_sequence: int = 0
    discontinuity_sequence: int = 0
    segments: list[UpstreamSegment] = field(default_factory=list)
    ad_ranges: list[AdRange] = field(default_factory=list)
    prefetch_uris: list[str] = field(default_factory=list)
    passthrough_tags: list[str] = field(default_factory=list)
    dropped_tags: list[str] = field(default_factory=list)
    has_endlist: bool = False
    is_low_latency: bool = False

    @property
    def ad_segment_count(self) -> int:
        return sum(1 for s in self.segments if s.is_ad)


@dataclass(slots=True)
class OutputSegment:
    """A segment we have committed to, with the sequence number we gave it."""

    seq: int
    uri: str
    duration: float
    key: str
    title: str = ""
    program_date_time: str | None = None
    byterange: str | None = None
    discontinuity: bool = False


# ---------------------------------------------------------------------- parse
def parse_media_playlist(
    playlist: str,
    base_url: str,
    *,
    strip_ads: bool = True,
    known_ad_ranges: Mapping[str, AdRange] | None = None,
    now: float = 0.0,
) -> ParsedPlaylist:
    """Parse a media playlist, marking (never dropping) stitched-ad segments.

    `known_ad_ranges` carries ad dateranges learned on *earlier* polls. Twitch's
    daterange tag scrolls out of the sliding window well before the ad segments
    it describes do, so without this memory the same ad is classified one way on
    one poll and another way on the next, and the output keeps shifting.
    """
    out = ParsedPlaylist()
    remembered = dict(known_ad_ranges or {})

    pending_pdt: str | None = None
    pending_byterange: str | None = None
    pending_duration = 0.0
    pending_title = ""
    pending_discontinuity = False
    have_extinf = False

    in_ad = False
    ad_remaining = 0.0
    ad_known_duration = False
    index = 0

    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#"):
            upper = line.upper()

            if upper.startswith("#EXT-X-VERSION:"):
                with suppress(ValueError):
                    out.version = int(line.split(":", 1)[1].strip())
                continue

            if upper.startswith("#EXT-X-TARGETDURATION:"):
                with suppress(ValueError):
                    out.target_duration = float(line.split(":", 1)[1].strip())
                continue

            if upper.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                with suppress(ValueError):
                    out.media_sequence = int(line.split(":", 1)[1].strip())
                continue

            if upper.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                with suppress(ValueError):
                    out.discontinuity_sequence = int(line.split(":", 1)[1].strip())
                continue

            if upper.startswith("#EXT-X-ENDLIST"):
                out.has_endlist = True
                continue

            if upper.startswith("#EXT-X-DATERANGE:"):
                attrs = parse_attributes(line.split(":", 1)[1])
                if is_ad_daterange(attrs):
                    rng = AdRange(
                        id=attrs.get("ID", f"anon-{index}"),
                        start_epoch=parse_iso8601(attrs.get("START-DATE")),
                        duration=_float_attr(attrs, "DURATION", "PLANNED-DURATION"),
                        first_seen=now,
                    )
                    out.ad_ranges.append(rng)
                    remembered.setdefault(rng.id, rng)
                    if strip_ads:
                        in_ad = True
                        ad_known_duration = rng.duration is not None
                        ad_remaining = (
                            rng.duration
                            if rng.duration is not None
                            else _UNKNOWN_AD_WINDOW_SECONDS
                        )
                continue

            if upper.startswith("#EXT-X-DISCONTINUITY"):
                if in_ad:
                    in_ad = False
                    ad_remaining = 0.0
                    ad_known_duration = False
                pending_discontinuity = True
                continue

            if upper.startswith("#EXTINF:"):
                pending_duration = _extinf_duration(line)
                pending_title = _extinf_title(line)
                have_extinf = True
                continue

            if upper.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_pdt = line.split(":", 1)[1].strip()
                continue

            if upper.startswith("#EXT-X-BYTERANGE:"):
                pending_byterange = line.split(":", 1)[1].strip()
                continue

            if upper.startswith("#EXT-X-TWITCH-PREFETCH:"):
                # Speculative, unnumbered, no EXTINF - it cannot take part in our
                # sequence numbering, and Twitch prefetches ad segments too.
                out.prefetch_uris.append(urljoin(base_url, line.split(":", 1)[1].strip()))
                continue

            if any(upper.startswith(prefix) for prefix in _DROPPED_TAG_PREFIXES):
                out.is_low_latency = True
                tag = line.split(":", 1)[0]
                if tag not in out.dropped_tags:
                    out.dropped_tags.append(tag)
                continue

            if upper.startswith("#EXT-X-KEY") or upper.startswith("#EXT-X-MAP"):
                attrs_body = line.split(":", 1)[1] if ":" in line else ""
                uri = parse_attributes(attrs_body).get("URI")
                if uri:
                    line = line.replace(f'URI="{uri}"', f'URI="{urljoin(base_url, uri)}"')
                out.passthrough_tags.append(line)
                continue

            continue

        # ------------------------------------------------------------ URI line
        if not have_extinf:
            # A bare URI with no EXTINF is not a segment we can schedule.
            continue

        pdt_epoch = parse_iso8601(pending_pdt)
        segment = UpstreamSegment(
            uri=urljoin(base_url, line),
            duration=pending_duration,
            index=index,
            upstream_seq=out.media_sequence + index,
            title=pending_title,
            program_date_time=pending_pdt,
            program_date_epoch=pdt_epoch,
            byterange=pending_byterange,
            discontinuity_before=pending_discontinuity,
        )

        if strip_ads:
            if in_ad:
                segment.is_ad = True
                segment.ad_source = "daterange"
                ad_remaining -= segment.duration or 2.0
                if ad_remaining <= 0.001:
                    in_ad = False
                    ad_known_duration = False
            elif pdt_epoch is not None and any(
                r.covers(pdt_epoch) for r in remembered.values()
            ):
                segment.is_ad = True
                segment.ad_source = "session-range"
            elif remembered and _AD_TITLE_RE.search(segment.title):
                # Only trusted once a real ad daterange has been seen for this
                # channel: cold, this heuristic causes total blackouts.
                segment.is_ad = True
                segment.ad_source = "title"

        out.segments.append(segment)
        index += 1

        pending_pdt = None
        pending_byterange = None
        pending_duration = 0.0
        pending_title = ""
        pending_discontinuity = False
        have_extinf = False

    # A duration-less ad window must not swallow the live edge: if we are still
    # inside one when the playlist ends, give the final segment back. An explicit
    # discontinuity would have closed the window properly.
    if in_ad and not ad_known_duration and out.segments:
        tail = out.segments[-1]
        if tail.is_ad and tail.ad_source == "daterange":
            tail.is_ad = False
            tail.ad_source = None

    return out


# --------------------------------------------------------------------- render
def render_media_playlist(
    segments: Sequence[OutputSegment],
    *,
    target_duration: float,
    media_sequence: int,
    discontinuity_sequence: int = 0,
    version: int = 3,
    passthrough_tags: Sequence[str] = (),
) -> str:
    """Render committed segments as a media playlist."""
    lines = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{version}",
        f"#EXT-X-TARGETDURATION:{max(1, int(round(target_duration)))}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
    ]
    if discontinuity_sequence:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}")
    lines.extend(passthrough_tags)

    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if seg.program_date_time:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg.program_date_time}")
        lines.append(f"#EXTINF:{seg.duration:.3f},{seg.title}")
        if seg.byterange:
            lines.append(f"#EXT-X-BYTERANGE:{seg.byterange}")
        lines.append(seg.uri)

    return "\n".join(lines) + "\n"


# --------------------------------------------------------- stateless compat
@dataclass
class PlaylistResult:
    text: str
    removed_segments: int = 0
    removed_seconds: float = 0.0
    segment_count: int = 0
    variants: list[str] = field(default_factory=list)

    @property
    def is_master(self) -> bool:
        return bool(self.variants)


def _default_rewriter(url: str) -> str:
    return url


def rewrite_playlist(
    playlist: str,
    base_url: str,
    *,
    strip_ads: bool = True,
    rewrite_uri: UriRewriter | None = None,
) -> PlaylistResult:
    """Stateless rewrite: absolutise URIs and drop ads, renumbering from zero.

    Retained for callers that genuinely have no session (and for tests). Live
    playback goes through `services.stream_session` instead, because a stateless
    rewrite cannot keep `#EXT-X-MEDIA-SEQUENCE` monotonic across polls.
    """
    rewriter = rewrite_uri or _default_rewriter
    parsed = parse_media_playlist(playlist, base_url, strip_ads=strip_ads)

    kept = [s for s in parsed.segments if not s.is_ad]
    # Never hand a player an empty playlist - that is a fatal error, whereas an
    # unstripped ad is merely annoying.
    if strip_ads and parsed.segments and not kept:
        kept = list(parsed.segments)

    removed = [s for s in parsed.segments if s not in kept]
    out: list[OutputSegment] = []
    prev_index: int | None = None
    for seq, seg in enumerate(kept):
        gap = prev_index is not None and seg.index != prev_index + 1
        out.append(
            OutputSegment(
                seq=seq,
                uri=rewriter(seg.uri),
                duration=seg.duration,
                key=seg.uri,
                title=seg.title,
                program_date_time=seg.program_date_time,
                byterange=seg.byterange,
                discontinuity=seg.discontinuity_before or gap,
            )
        )
        prev_index = seg.index

    text = render_media_playlist(
        out,
        target_duration=parsed.target_duration,
        media_sequence=parsed.media_sequence,
        discontinuity_sequence=parsed.discontinuity_sequence,
        version=parsed.version or 3,
        passthrough_tags=parsed.passthrough_tags,
    )

    return PlaylistResult(
        text=text,
        removed_segments=len(removed),
        removed_seconds=round(sum(s.duration for s in removed), 3),
        segment_count=len(out),
    )


def rewrite_master(
    playlist: str, base_url: str, *, rewrite_uri: UriRewriter | None = None
) -> PlaylistResult:
    """Absolutise (or redirect) variant URIs in a master playlist."""
    rewriter = rewrite_uri or _default_rewriter
    out: list[str] = []
    variants: list[str] = []

    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXT-X-MEDIA:") or line.upper().startswith(
                "#EXT-X-I-FRAME-STREAM-INF:"
            ):
                attrs = parse_attributes(line.split(":", 1)[1])
                uri = attrs.get("URI")
                if uri:
                    absolute = rewriter(urljoin(base_url, uri))
                    line = line.replace(f'URI="{uri}"', f'URI="{absolute}"')
            out.append(line)
            continue
        absolute = rewriter(urljoin(base_url, line))
        variants.append(absolute)
        out.append(absolute)

    return PlaylistResult(text="\n".join(out) + "\n", variants=variants)


def is_master_playlist(playlist: str) -> bool:
    return "#EXT-X-STREAM-INF" in playlist
