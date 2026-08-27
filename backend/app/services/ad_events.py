"""Ad-progress signalling for blocked breaks, ported from TTV-AB.

Technique adapted from TTV-AB by GosuDRM - https://github.com/GosuDRM/TTV-AB
(MIT-based licence with attribution). No source was copied.

When a break is blocked, Twitch never receives the telemetry its own player
would have sent, and that absence is one of the things anti-adblock looks for.
This module replays those events for ads that were skipped: an impression, four
quartile completions, and a pod completion.

Be clear about what that means - it reports ads as watched that were not. It is
separate from blocking, off-by-a-toggle, and failing here must never affect
playback, which is why every call is fire-and-forget.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging_conf import get_logger
from app.services import http as shared_http
from app.services.hls import parse_attributes

log = get_logger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"

# Twitch's public web client id - the same one its own player sends.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# Persisted-query hash for ClientSideAdEventHandling_RecordAdEvent. Twitch
# rotates these occasionally; a stale hash makes the call fail, which by design
# costs nothing but a debug log.
RECORD_AD_EVENT_HASH = "7e6c69e6eb59f8ccb97ab73686f3d8b7d85a72a0298745ccd8bfc68e4054ca5b"

_QUARTILES = (1, 2, 3, 4)

# Ads already reported, so a pod lingering in the sliding window is not counted
# repeatedly. Bounded because a long-lived process would otherwise grow it
# without limit.
_reported: set[str] = set()
_REPORTED_MAX = 500


def _remember(ad_id: str) -> bool:
    """Record an ad as reported. False if it already was."""
    if ad_id in _reported:
        return False
    if len(_reported) >= _REPORTED_MAX:
        _reported.clear()
    _reported.add(ad_id)
    return True


def parse_ad_dateranges(playlist: str) -> list[dict[str, str]]:
    """Pull the stitched-ad DATERANGE attribute sets out of a playlist."""
    out: list[dict[str, str]] = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line.upper().startswith("#EXT-X-DATERANGE:"):
            continue
        attrs = parse_attributes(line.split(":", 1)[1])
        if attrs.get("ID", "").startswith("stitched-ad-"):
            out.append(attrs)
    return out


def build_packets(attrs: dict[str, str], pod_length: int) -> list[dict[str, Any]] | None:
    """Build the event batch for one ad, or None if it cannot be reported.

    The RADS token is what ties the events to a real ad impression; without one
    there is nothing meaningful to send, so those ads are skipped rather than
    reported with a blank.
    """
    rad_token = attrs.get("X-TV-TWITCH-AD-RADS-TOKEN", "")
    ad_id = attrs.get("ID", "")
    if not rad_token or not ad_id:
        return None

    try:
        duration = float(attrs.get("X-TV-TWITCH-AD-DURATION") or attrs.get("DURATION") or 0)
    except ValueError:
        duration = 0.0
    try:
        position = int(attrs.get("X-TV-TWITCH-AD-POD-POSITION", "0"))
    except ValueError:
        position = 0

    payload = {
        "stitched": True,
        "ad_id": ad_id,
        "roll_type": attrs.get("X-TV-TWITCH-AD-ROLL-TYPE", ""),
        "creative_id": attrs.get("X-TV-TWITCH-AD-CREATIVE-ID", ""),
        "order_id": attrs.get("X-TV-TWITCH-AD-ORDER-ID", ""),
        "line_item_id": attrs.get("X-TV-TWITCH-AD-LINE-ITEM-ID", ""),
        "player_mute": False,
        "player_volume": 1.0,
        "visible": True,
        "duration": duration,
        "ad_position": position,
        "total_ads": pod_length,
    }

    def packet(event: str, **extra: Any) -> dict[str, Any]:
        return {
            "operationName": "ClientSideAdEventHandling_RecordAdEvent",
            "variables": {
                "input": {
                    "eventName": event,
                    "eventPayload": json.dumps({**payload, **extra}),
                    "radToken": rad_token,
                }
            },
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": RECORD_AD_EVENT_HASH}
            },
        }

    batch = [packet("video_ad_impression")]
    batch += [packet("video_ad_quartile_complete", quartile=q) for q in _QUARTILES]
    batch.append(packet("video_ad_pod_complete"))
    return batch


async def report_blocked_ads(
    playlist: str, *, user_token: str | None = None, device_id: str = "oauth"
) -> int:
    """Report every not-yet-reported stitched ad in this playlist.

    Returns how many ads were reported. Never raises: this is telemetry for
    Twitch's benefit, and playback must not depend on it.
    """
    dateranges = parse_ad_dateranges(playlist)
    if not dateranges:
        return 0

    pod_length = 0
    for attrs in dateranges:
        try:
            pod_length = max(pod_length, int(attrs.get("X-TV-TWITCH-AD-POD-LENGTH", "0")))
        except ValueError:
            continue
    pod_length = pod_length or len(dateranges)

    batch: list[dict[str, Any]] = []
    reported = 0
    for attrs in dateranges:
        ad_id = attrs.get("ID", "")
        if not _remember(ad_id):
            continue
        packets = build_packets(attrs, pod_length)
        if packets is None:
            continue
        batch.extend(packets)
        reported += 1

    if not batch:
        return 0

    headers = {
        "Client-ID": CLIENT_ID,
        "X-Device-Id": device_id,
        "Content-Type": "text/plain; charset=UTF-8",
    }
    if user_token:
        headers["Authorization"] = f"OAuth {user_token}"

    try:
        response = await shared_http.get_client().post(
            GQL_URL, json=batch, headers=headers, timeout=5.0
        )
        if response.status_code != 200:
            log.debug("ad event report rejected", status=response.status_code)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break playback
        log.debug("ad event report failed", error=str(exc)[:160])
        return 0

    log.info("reported blocked ads to twitch", ads=reported, pod_length=pod_length)
    return reported
