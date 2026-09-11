"""The shape of the ad telemetry we replay for a break we blocked.

These assertions are about plausibility, not correctness in any functional
sense: the events go to Twitch, nothing here reads them back, and the only way
they can fail is by describing a viewing session no real player would produce.
So the tests pin the fields that used to say something impossible.
"""

from __future__ import annotations

import json

from app.services.ad_events import build_packets, parse_ad_dateranges

POD = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-DATERANGE:ID="stitched-ad-1700000000",CLASS="twitch-stitched-ad",\
START-DATE="2026-01-01T00:00:00.000Z",DURATION=15.023,\
X-TV-TWITCH-AD-ROLL-TYPE="MIDROLL",X-TV-TWITCH-AD-POD-LENGTH="2",\
X-TV-TWITCH-AD-POD-POSITION="0",X-TV-TWITCH-AD-RADS-TOKEN="rads-token-abc",\
X-TV-TWITCH-AD-ADVERTISER-ID="adv-1",X-TV-TWITCH-AD-CREATIVE-ID="cre-2",\
X-TV-TWITCH-AD-LINE-ITEM-ID="line-3",X-TV-TWITCH-AD-ORDER-ID="ord-4",\
X-TV-TWITCH-AD-AD-SESSION-ID="sess-5",X-TV-TWITCH-AD-AD-FORMAT="standard"
#EXTINF:2.000,
ad0.ts
"""


def _payloads(batch):
    return {
        p["variables"]["input"]["eventName"]: json.loads(
            p["variables"]["input"]["eventPayload"]
        )
        for p in batch
    }


def _attrs():
    ranges = parse_ad_dateranges(POD)
    assert len(ranges) == 1
    return ranges[0]


def test_the_batch_is_an_impression_four_quartiles_and_a_pod_completion():
    batch = build_packets(_attrs(), pod_length=2)

    assert batch is not None
    names = [p["variables"]["input"]["eventName"] for p in batch]
    assert names == [
        "video_ad_impression",
        *["video_ad_quartile_complete"] * 4,
        "video_ad_pod_complete",
    ]
    assert [
        json.loads(p["variables"]["input"]["eventPayload"])["quartile"]
        for p in batch
        if p["variables"]["input"]["eventName"] == "video_ad_quartile_complete"
    ] == [1, 2, 3, 4]
    assert all(
        p["variables"]["input"]["radToken"] == "rads-token-abc" for p in batch
    ), "the RADS token is what ties these events to the real impression"


def test_the_ad_position_counts_from_one():
    """`X-TV-TWITCH-AD-POD-POSITION` is zero-based; the event field is not.

    Passing the attribute through unchanged reported the first ad of every pod
    at position 0, which no real player ever sends.
    """
    payloads = _payloads(build_packets(_attrs(), pod_length=2))

    assert payloads["video_ad_impression"]["ad_position"] == 1
    assert payloads["video_ad_impression"]["total_ads"] == 2


def test_the_duration_is_whole_seconds():
    """A float carrying the playlist's DURATION to three places is a tell."""
    payloads = _payloads(build_packets(_attrs(), pod_length=2))
    duration = payloads["video_ad_impression"]["duration"]

    assert duration == 15
    assert isinstance(duration, int)


def test_the_player_is_described_as_muted_but_on_screen():
    """The claim has to be one a tuner could plausibly make.

    This previously reported an unmuted player at full volume, which is a
    stronger assertion about a viewer than anything going through Jellyfin's
    transcoder can support.
    """
    payloads = _payloads(build_packets(_attrs(), pod_length=2))
    impression = payloads["video_ad_impression"]

    assert impression["player_mute"] is True
    assert impression["player_volume"] == 0.5
    assert impression["visible"] is True
    assert impression["stitched"] is True
    assert impression["roll_type"] == "midroll"


def test_the_pod_completion_carries_the_two_fields_only_it_has():
    """Omitting these left the event that closes the break the least convincing."""
    payloads = _payloads(build_packets(_attrs(), pod_length=2))

    assert payloads["video_ad_pod_complete"]["ad_session_id"] == "sess-5"
    assert payloads["video_ad_pod_complete"]["format_name"] == "standard"
    assert "ad_session_id" not in payloads["video_ad_impression"]
    assert "format_name" not in payloads["video_ad_impression"]


def test_the_creative_identifiers_are_passed_through():
    payloads = _payloads(build_packets(_attrs(), pod_length=2))
    impression = payloads["video_ad_impression"]

    assert impression["ad_id"] == "stitched-ad-1700000000"
    assert impression["creative_id"] == "cre-2"
    assert impression["line_item_id"] == "line-3"
    assert impression["order_id"] == "ord-4"


def test_an_ad_with_no_rads_token_is_skipped_rather_than_reported_blank():
    """Without the token there is nothing meaningful to send."""
    attrs = dict(_attrs())
    del attrs["X-TV-TWITCH-AD-RADS-TOKEN"]

    assert build_packets(attrs, pod_length=2) is None
