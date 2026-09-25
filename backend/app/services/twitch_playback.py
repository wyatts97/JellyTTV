"""Playback tokens minted directly from Twitch, without spawning streamlink.

streamlink is a whole process per resolve: reliable, and far too slow to sit in
the path of an ad break. Covering a break means finding a clean copy of the
stream *now*, and the search only works if a candidate costs milliseconds.

This module does what Twitch's own web player does, and what the browser
ad-block extensions (VAFT / `twitch-videoad`, and TTV-AB before it) do: ask
GraphQL for a `PlaybackAccessToken`, hand it to usher, and read the master
playlist that comes back. The technique is reimplemented, not copied.

Two properties make it useful beyond speed:

* **Ads are stitched per token, not per channel.** A token minted for a
  different `playerType` on the same channel usually comes back clean, and -
  unlike `picture-by-picture` - `embed` and `popout` come back with the *whole*
  rendition ladder, so a break can be covered without changing resolution.
* **The master playlist is reusable.** It lists every rendition, so the same
  fetch that finds a clean stream also picks the variant matching what is
  already playing. Cached per channel and player type, a break is covered from
  warm state.

Everything Twitch-private lives here - the client id, the persisted query hash,
the usher parameters - so an API change has one place to be fixed. When it does
break, `resolver` falls back to streamlink and playback continues.
"""

from __future__ import annotations

import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx

from app.logging_conf import get_logger
from app.services import http as shared_http
from app.services.hls import parse_attributes

log = get_logger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
USHER_URL = "https://usher.ttvnw.net/api/channel/hls/{login}.m3u8"

# Twitch's public web client id - the same one its own player sends.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# Persisted-query hash for PlaybackAccessToken. Twitch rotates these
# occasionally; a stale hash fails the request, which falls back to streamlink.
PLAYBACK_ACCESS_TOKEN_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"

# `autoplay` is only minted for the android platform - that is what it means -
# and asking for it as `web` returns a token for a ladder that does not exist.
ANDROID_PLAYER_TYPES = frozenset({"autoplay"})

REQUEST_TIMEOUT = 6.0
# A master playlist stays usable for as long as its usher token does. Kept short
# anyway: the point of the cache is covering a break from warm state, not
# holding a url long enough for it to go stale mid-switch.
MASTER_TTL_SECONDS = 60.0

# Source group id in every Twitch master playlist: the untranscoded rendition.
SOURCE_GROUP = "chunked"
AUDIO_ONLY_GROUP = "audio_only"


class PlaybackError(RuntimeError):
    """The playback token or master playlist could not be obtained."""


class ChannelOffline(PlaybackError):
    """Twitch has no stream to mint a token for."""


@dataclass(frozen=True, slots=True)
class Variant:
    """One rendition from a master playlist."""

    url: str
    group_id: str
    name: str
    bandwidth: int
    codecs: str
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None

    @property
    def is_video(self) -> bool:
        return self.height is not None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def quality(self) -> str:
        """streamlink's spelling of this rendition: 1080p60, 360p, best."""
        if self.group_id == SOURCE_GROUP:
            return "best"
        if self.group_id == AUDIO_ONLY_GROUP:
            return "audio_only"
        return self.name.replace(" (source)", "").strip() or self.group_id

    def matches(self, other: Variant) -> bool:
        """Same picture: identical resolution and frame rate."""
        return (
            self.width == other.width
            and self.height == other.height
            and _same_frame_rate(self.frame_rate, other.frame_rate)
        )


@dataclass(slots=True)
class Master:
    login: str
    player_type: str
    variants: list[Variant]
    fetched_at: float = field(default_factory=time.monotonic)

    def expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return now - self.fetched_at > MASTER_TTL_SECONDS


def _same_frame_rate(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left == right
    # Twitch reports 30.000 and 59.999-ish; whole-number comparison is the
    # honest precision here.
    return abs(left - right) < 1.0


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_master(text: str) -> list[Variant]:
    """Read the renditions out of a Twitch master playlist.

    `#EXT-X-MEDIA` carries the human name of each group, `#EXT-X-STREAM-INF` the
    resolution and codecs, and the line after it the url.
    """
    names: dict[str, str] = {}
    variants: list[Variant] = []
    lines = text.replace("\r", "").split("\n")
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attributes(line.split(":", 1)[1])
            group = attrs.get("GROUP-ID", "")
            if group:
                names[group] = attrs.get("NAME", group)
            continue
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        if index + 1 >= len(lines):
            continue
        url = lines[index + 1].strip()
        if not url or url.startswith("#"):
            continue
        attrs = parse_attributes(line.split(":", 1)[1])
        width = height = None
        resolution = attrs.get("RESOLUTION")
        if resolution and "x" in resolution:
            try:
                width, height = (int(part) for part in resolution.lower().split("x", 1))
            except ValueError:
                width = height = None
        group = attrs.get("VIDEO", "")
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            bandwidth = 0
        variants.append(
            Variant(
                url=url,
                group_id=group,
                name=names.get(group, group),
                bandwidth=bandwidth,
                codecs=attrs.get("CODECS", ""),
                width=width,
                height=height,
                frame_rate=_float(attrs.get("FRAME-RATE")),
            )
        )
    return variants


def pick_variant(
    variants: list[Variant], *, quality: str = "best", match: Variant | None = None
) -> Variant | None:
    """Choose a rendition: the one already playing if possible, else by quality.

    `match` is what the session is currently serving. Keeping resolution *and*
    frame rate identical across a switch is the whole point - a mid-stream
    resolution change costs a buffer re-initialisation in every player, which is
    exactly the stutter a covered ad break is supposed to avoid.
    """
    video = [v for v in variants if v.is_video]
    if not video:
        # An audio-only ladder is still playable; better than nothing.
        return variants[0] if variants else None

    if match is not None:
        exact = [v for v in video if v.matches(match)]
        if exact:
            return exact[0]
        # Nothing identical: the closest picture, largest first on a tie, so a
        # fallback degrades as little as it has to.
        return min(video, key=lambda v: (abs(v.pixels - match.pixels), -v.pixels))

    wanted = (quality or "best").strip().lower()
    if wanted in {"best", "source", ""}:
        return max(video, key=lambda v: (v.pixels, v.bandwidth))
    if wanted == "worst":
        return min(video, key=lambda v: (v.pixels, v.bandwidth))
    if wanted == "audio_only":
        audio = [v for v in variants if not v.is_video]
        return audio[0] if audio else None

    named = [v for v in video if v.quality.lower() == wanted]
    if named:
        return named[0]
    # A quality this ladder does not carry - `1080p` without the frame rate, or
    # 720p on `picture-by-picture`, which tops out at 360p. Take the tallest
    # rendition at or below the request, and the smallest if even that is taller.
    digits = "".join(ch for ch in wanted if ch.isdigit())
    if digits:
        target = int(digits[:4])
        at_or_below = [v for v in video if (v.height or 0) <= target]
        if at_or_below:
            return max(at_or_below, key=lambda v: (v.pixels, v.bandwidth))
        return min(video, key=lambda v: (v.pixels, v.bandwidth))
    return max(video, key=lambda v: (v.pixels, v.bandwidth))


async def access_token(
    login: str,
    player_type: str,
    *,
    user_token: str | None = None,
    device_id: str | None = None,
) -> tuple[str, str]:
    """Mint a playback token for one channel and player type: (signature, value)."""
    platform = "android" if player_type in ANDROID_PLAYER_TYPES else "web"
    body = {
        "operationName": "PlaybackAccessToken",
        "variables": {
            "isLive": True,
            "login": login,
            "isVod": False,
            "vodID": "",
            "playerType": player_type,
            "platform": platform,
        },
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": PLAYBACK_ACCESS_TOKEN_HASH}
        },
    }
    headers = {"Client-ID": CLIENT_ID}
    # The install's stable device id, on both spellings Twitch accepts - the same
    # one the ad-event report sends, so the viewer asking for the stream and the
    # viewer reporting the ad are one device (see services.ad_events).
    if device_id:
        headers["X-Device-Id"] = device_id
        headers["Device-ID"] = device_id
    if user_token:
        # A subscribed or Turbo account is served a genuinely ad-free stream.
        headers["Authorization"] = f"OAuth {user_token}"

    try:
        response = await shared_http.get_client().post(
            GQL_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise PlaybackError(f"playback token request failed: {exc}") from exc

    if response.status_code != 200:
        raise PlaybackError(f"playback token request returned {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlaybackError("playback token response was not json") from exc

    if payload.get("errors"):
        raise PlaybackError(f"playback token rejected: {payload['errors']}")
    token = (payload.get("data") or {}).get("streamPlaybackAccessToken")
    if not token:
        # Twitch answers a token request for an offline channel with a null.
        raise ChannelOffline(f"{login} has no playback token (offline?)")
    signature = token.get("signature")
    value = token.get("value")
    if not signature or not value:
        raise PlaybackError("playback token was incomplete")
    return signature, value


def usher_params(signature: str, token: str) -> dict[str, str]:
    """The query Twitch's own player sends with a playback token."""
    return {
        "sig": signature,
        "token": token,
        "allow_source": "true",
        "allow_audio_only": "true",
        "fast_bread": "true",
        "player": "twitchweb",
        "playlist_include_framerate": "true",
        # H.264 only: an HEVC rendition appearing on one side of a switch and not
        # the other is a codec change mid-stream, which is the thing this module
        # exists to avoid.
        "supported_codecs": "avc1",
        "p": str(random.randint(1_000_000, 9_999_999)),
    }


async def fetch_master(
    login: str,
    player_type: str,
    *,
    user_token: str | None = None,
    device_id: str | None = None,
) -> Master:
    """Mint a token and read the master playlist it unlocks."""
    signature, token = await access_token(
        login, player_type, user_token=user_token, device_id=device_id
    )
    try:
        response = await shared_http.get_client().get(
            USHER_URL.format(login=login),
            params=usher_params(signature, token),
            headers=shared_http.UPSTREAM_HEADERS,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise PlaybackError(f"usher request failed: {exc}") from exc

    if response.status_code == 404:
        raise ChannelOffline(f"{login} is offline")
    if response.status_code != 200:
        raise PlaybackError(f"usher returned {response.status_code}")

    variants = parse_master(response.text)
    if not variants:
        raise PlaybackError("master playlist carried no renditions")
    _remember(variants)
    return Master(login=login, player_type=player_type, variants=variants)


# ------------------------------------------------------------------- cache
_masters: dict[tuple[str, str], Master] = {}

# Every variant url we have ever handed out, so a caller holding only a url can
# ask what picture it is. That is how the backup search knows which rendition to
# match without threading the variant through the session and the router.
_variants_by_url: OrderedDict[str, Variant] = OrderedDict()
_VARIANT_MEMORY = 512


def _remember(variants: list[Variant]) -> None:
    for variant in variants:
        _variants_by_url[variant.url] = variant
        _variants_by_url.move_to_end(variant.url)
    while len(_variants_by_url) > _VARIANT_MEMORY:
        _variants_by_url.popitem(last=False)


def variant_for_url(url: str | None) -> Variant | None:
    """What rendition is this url? None for a url we did not resolve.

    A url resolved by streamlink is not in here, and the caller then has nothing
    to match against - it falls back to choosing by quality.

    A hit counts as use. Every backup search adds a whole ladder per player
    type, so without this the url a session is *playing* - looked up on every
    search, but minted once - aged out after a quarter of an hour and every
    later break was searched with nothing to match.
    """
    if not url:
        return None
    variant = _variants_by_url.get(url)
    if variant is not None:
        _variants_by_url.move_to_end(url)
    return variant


def _key(login: str, player_type: str) -> tuple[str, str]:
    return (login.lower(), player_type)


async def master(
    login: str,
    player_type: str,
    *,
    user_token: str | None = None,
    device_id: str | None = None,
    force: bool = False,
) -> Master:
    """A master playlist for this channel and player type, cached briefly.

    The cache is what makes the *first* ad break as fast as the fifth: the
    session warms it while the stream is playing, so covering a break costs one
    playlist fetch rather than a token round trip as well.
    """
    key = _key(login, player_type)
    cached = _masters.get(key)
    if cached is not None and not force and not cached.expired():
        return cached
    fresh = await fetch_master(
        login, player_type, user_token=user_token, device_id=device_id
    )
    _masters[key] = fresh
    return fresh


def invalidate(login: str | None = None, player_type: str | None = None) -> None:  # noqa: D401
    """Drop cached masters. Called when a variant url stops working."""
    if login is None:
        _masters.clear()
        return
    lowered = login.lower()
    stale = [
        key
        for key in _masters
        if key[0] == lowered and (player_type is None or key[1] == player_type)
    ]
    for key in stale:
        _masters.pop(key, None)
