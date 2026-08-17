"""HLS playlist rewriting, including Twitch stitched-ad removal.

Twitch splices ads directly into the live media playlist. The ad window is
announced with an `#EXT-X-DATERANGE` tag; the segments that follow it are ad
content. streamlink identifies those dateranges with this predicate:

    CLASS == "twitch-stitched-ad"
    or ID starts with "stitched-ad-"
    or any attribute key starts with "X-TV-TWITCH-AD-"

We reimplement that here and drop segments until the announced duration has been
consumed (or an explicit `#EXT-X-DISCONTINUITY` closes the window), inserting a
single discontinuity so the decoder resets cleanly.

This is a heuristic against an undocumented, changing format - it lives in one
small module on purpose so it is cheap to test and cheap to fix.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')

AD_CLASS = "twitch-stitched-ad"
AD_ID_PREFIX = "stitched-ad-"
AD_ATTR_PREFIX = "X-TV-TWITCH-AD-"
# Fallback: Twitch sometimes labels ad segments in the EXTINF title.
_AD_TITLE_RE = re.compile(r"(amazon|stitched-ad|twitch-ad)", re.IGNORECASE)

# Safety net if a daterange announces no duration at all.
_MAX_AD_WINDOW_SECONDS = 240.0

UriRewriter = Callable[[str], str]


def parse_attributes(value: str) -> dict[str, str]:
    """Parse an HLS tag attribute list into a dict (quotes stripped)."""
    attrs: dict[str, str] = {}
    for key, raw in _ATTR_RE.findall(value):
        attrs[key.upper()] = raw.strip('"')
    return attrs


def is_ad_daterange(attrs: dict[str, str]) -> bool:
    if attrs.get("CLASS", "").lower() == AD_CLASS:
        return True
    if attrs.get("ID", "").startswith(AD_ID_PREFIX):
        return True
    return any(key.startswith(AD_ATTR_PREFIX) for key in attrs)


def _daterange_duration(attrs: dict[str, str]) -> float:
    for key in ("DURATION", "PLANNED-DURATION"):
        raw = attrs.get(key)
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return _MAX_AD_WINDOW_SECONDS


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
    """Rewrite a media playlist: absolutise URIs and optionally drop ads."""
    rewriter = rewrite_uri or _default_rewriter
    out: list[str] = []
    pending: list[str] = []

    in_ad = False
    ad_remaining = 0.0
    need_discontinuity = False
    removed_segments = 0
    removed_seconds = 0.0
    segment_count = 0

    def flush_pending() -> None:
        nonlocal need_discontinuity
        if need_discontinuity:
            out.append("#EXT-X-DISCONTINUITY")
            need_discontinuity = False
        out.extend(pending)
        pending.clear()

    for raw_line in playlist.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#"):
            upper = line.upper()

            if strip_ads and upper.startswith("#EXT-X-DATERANGE:"):
                attrs = parse_attributes(line.split(":", 1)[1])
                if is_ad_daterange(attrs):
                    in_ad = True
                    ad_remaining = _daterange_duration(attrs)
                    pending.clear()
                    continue
                out.append(line)
                continue

            if upper.startswith("#EXT-X-DISCONTINUITY"):
                if in_ad:
                    in_ad = False
                    ad_remaining = 0.0
                    need_discontinuity = True
                    pending.clear()
                    continue
                out.append(line)
                continue

            if upper.startswith("#EXTINF:"):
                if strip_ads and not in_ad and _AD_TITLE_RE.search(_extinf_title(line)):
                    in_ad = True
                    ad_remaining = max(ad_remaining, _extinf_duration(line))
                if in_ad:
                    pending.clear()
                    pending.append(line)
                    continue
                pending.append(line)
                continue

            if upper.startswith("#EXT-X-PROGRAM-DATE-TIME") or upper.startswith("#EXT-X-BYTERANGE"):
                if in_ad:
                    continue
                pending.append(line)
                continue

            if upper.startswith("#EXT-X-TWITCH-PREFETCH:"):
                if in_ad:
                    continue
                target = line.split(":", 1)[1].strip()
                out.append(f"#EXT-X-TWITCH-PREFETCH:{rewriter(urljoin(base_url, target))}")
                continue

            if upper.startswith("#EXT-X-KEY") or upper.startswith("#EXT-X-MAP"):
                attrs_body = line.split(":", 1)[1] if ":" in line else ""
                attrs = parse_attributes(attrs_body)
                uri = attrs.get("URI")
                if uri:
                    absolute = rewriter(urljoin(base_url, uri))
                    line = line.replace(f'URI="{uri}"', f'URI="{absolute}"')
                out.append(line)
                continue

            out.append(line)
            continue

        # ------------------------------------------------------------ URI line
        if in_ad:
            duration = _extinf_duration(pending[0]) if pending else 0.0
            removed_segments += 1
            removed_seconds += duration
            ad_remaining -= duration if duration else 2.0
            pending.clear()
            if ad_remaining <= 0.001:
                in_ad = False
                ad_remaining = 0.0
                need_discontinuity = True
            continue

        segment_count += 1
        flush_pending()
        out.append(rewriter(urljoin(base_url, line)))

    flush_pending()

    return PlaylistResult(
        text="\n".join(out) + "\n",
        removed_segments=removed_segments,
        removed_seconds=round(removed_seconds, 3),
        segment_count=segment_count,
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
