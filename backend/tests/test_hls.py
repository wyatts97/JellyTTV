"""Ad-stripping is the most fragile part of the app, so it gets the most tests."""

from __future__ import annotations

from app.services.hls import (
    is_ad_daterange,
    is_master_playlist,
    parse_attributes,
    parse_media_playlist,
    rewrite_master,
    rewrite_playlist,
)

BASE = "https://video-weaver.example.hls.ttvnw.net/v1/playlist/abc.m3u8"

CLEAN_MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:2.000,
seg100.ts
#EXTINF:2.000,
seg101.ts
#EXTINF:2.000,
seg102.ts
"""

MEDIA_WITH_STITCHED_ADS = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:2.000,
seg100.ts
#EXT-X-DATERANGE:ID="stitched-ad-1699999999",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00.000Z",DURATION=6.0,X-TV-TWITCH-AD-ROLL-TYPE="MIDROLL"
#EXTINF:2.000,
ad0.ts
#EXTINF:2.000,
ad1.ts
#EXTINF:2.000,
ad2.ts
#EXTINF:2.000,
seg101.ts
#EXTINF:2.000,
seg102.ts
"""

MEDIA_AD_ENDED_BY_DISCONTINUITY = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:2.000,
seg100.ts
#EXT-X-DATERANGE:ID="stitched-ad-42",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00.000Z"
#EXTINF:2.000,
ad0.ts
#EXTINF:2.000,
ad1.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.000,
seg101.ts
"""

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="video-weaver.example"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60",AUTOSELECT=YES,DEFAULT=YES,URI="chunked/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.4D402A,mp4a.40.2",VIDEO="chunked"
chunked/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p60"
720p60/index.m3u8
"""


def test_parse_attributes_handles_quotes_and_numbers():
    attrs = parse_attributes('ID="stitched-ad-1",CLASS="twitch-stitched-ad",DURATION=6.0')
    assert attrs["ID"] == "stitched-ad-1"
    assert attrs["CLASS"] == "twitch-stitched-ad"
    assert attrs["DURATION"] == "6.0"


def test_ad_detection_matches_streamlink_predicate():
    assert is_ad_daterange({"CLASS": "twitch-stitched-ad"})
    assert is_ad_daterange({"ID": "stitched-ad-999"})
    assert is_ad_daterange({"X-TV-TWITCH-AD-ROLL-TYPE": "PREROLL"})
    assert not is_ad_daterange({"CLASS": "chapter", "ID": "chapter-1"})


def test_clean_playlist_is_untouched_apart_from_absolutising():
    result = rewrite_playlist(CLEAN_MEDIA, BASE)
    assert result.removed_segments == 0
    assert result.segment_count == 3
    assert "https://video-weaver.example.hls.ttvnw.net/v1/playlist/seg100.ts" in result.text
    assert "#EXT-X-TARGETDURATION:2" in result.text


def test_stitched_ads_are_removed_and_discontinuity_inserted():
    result = rewrite_playlist(MEDIA_WITH_STITCHED_ADS, BASE)

    assert result.removed_segments == 3
    assert result.removed_seconds == 6.0
    assert result.segment_count == 3
    assert "ad0.ts" not in result.text
    assert "ad1.ts" not in result.text
    assert "ad2.ts" not in result.text
    assert "seg100.ts" in result.text
    assert "seg101.ts" in result.text
    assert "twitch-stitched-ad" not in result.text
    assert result.text.count("#EXT-X-DISCONTINUITY") == 1


def test_ad_window_can_be_closed_by_discontinuity():
    result = rewrite_playlist(MEDIA_AD_ENDED_BY_DISCONTINUITY, BASE)
    assert "ad0.ts" not in result.text
    assert "ad1.ts" not in result.text
    assert "seg101.ts" in result.text
    assert result.removed_segments == 2


def test_ads_are_kept_when_stripping_is_disabled():
    result = rewrite_playlist(MEDIA_WITH_STITCHED_ADS, BASE, strip_ads=False)
    assert result.removed_segments == 0
    assert "ad0.ts" in result.text
    # The DATERANGE tag itself is never echoed: we render from parsed segments,
    # and a marker describing a window we did not reproduce byte-for-byte would
    # only mislead the player.
    assert "twitch-stitched-ad" not in result.text


def test_segments_can_be_routed_through_a_rewriter():
    def rewrite(url: str) -> str:
        return f"https://jellyttv.local/hls/x/seg?u={url}"

    result = rewrite_playlist(CLEAN_MEDIA, BASE, rewrite_uri=rewrite)
    assert result.text.count("https://jellyttv.local/hls/x/seg?u=") == 3


def test_prefetch_tags_are_collected_but_never_emitted():
    """Prefetch entries are speculative and carry no EXTINF.

    They cannot be given a sequence number, and Twitch prefetches ad segments
    too - emitting them would smuggle back the ads we just stripped.
    """
    playlist = MEDIA_WITH_STITCHED_ADS + "#EXT-X-TWITCH-PREFETCH:next.ts\n"
    result = rewrite_playlist(playlist, BASE)
    assert "#EXT-X-TWITCH-PREFETCH" not in result.text
    assert "next.ts" not in result.text

    parsed = parse_media_playlist(playlist, BASE)
    assert parsed.prefetch_uris == [
        "https://video-weaver.example.hls.ttvnw.net/v1/playlist/next.ts"
    ]


# ------------------------------------------------- regressions: total blackout
MEDIA_DURATIONLESS_AD = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="stitched-ad-7",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00.000Z"
#EXTINF:2.000,
ad0.ts
#EXTINF:2.000,
ad1.ts
#EXTINF:2.000,
ad2.ts
"""

MEDIA_AMAZON_IN_TITLE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:2.000,Amazon haul unboxing
seg100.ts
#EXTINF:2.000,Amazon haul unboxing
seg101.ts
"""

MEDIA_LOW_LATENCY = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=1.0
#EXT-X-PART-INF:PART-TARGET=0.5
#EXTINF:2.000,
seg100.ts
#EXT-X-PART:DURATION=0.5,URI="part0.ts"
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part1.ts"
#EXT-X-RENDITION-REPORT:URI="../720p60/index.m3u8",LAST-MSN=101
"""


def test_duration_less_daterange_does_not_empty_the_playlist():
    """A DATERANGE with no DURATION used to strip the entire window.

    Zero segments is a fatal error for ffmpeg - this is the "will not connect at
    all" symptom. The live edge must survive.
    """
    result = rewrite_playlist(MEDIA_DURATIONLESS_AD, BASE)
    assert result.segment_count >= 1
    assert result.text.strip().endswith("ad2.ts")


def test_all_ads_falls_back_to_passthrough_rather_than_empty():
    parsed = parse_media_playlist(MEDIA_DURATIONLESS_AD, BASE)
    # Force the pathological case: everything marked as an ad.
    for seg in parsed.segments:
        seg.is_ad = True
    result = rewrite_playlist(MEDIA_DURATIONLESS_AD, BASE)
    assert result.segment_count > 0


def test_ad_title_heuristic_ignores_the_word_amazon_in_a_stream_title():
    """`amazon` alone used to match, blanking any stream about Amazon."""
    result = rewrite_playlist(MEDIA_AMAZON_IN_TITLE, BASE)
    assert result.removed_segments == 0
    assert result.segment_count == 2


def test_non_ad_daterange_with_twitch_attributes_is_kept():
    assert not is_ad_daterange(
        {"CLASS": "twitch-trigger", "X-TV-TWITCH-AD-SIGNAL-ID": "abc"}
    )


def test_low_latency_tags_are_dropped_not_emitted_with_relative_uris():
    parsed = parse_media_playlist(MEDIA_LOW_LATENCY, BASE)
    assert parsed.is_low_latency
    assert "#EXT-X-PART" in parsed.dropped_tags

    result = rewrite_playlist(MEDIA_LOW_LATENCY, BASE)
    for tag in ("#EXT-X-PART", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT"):
        assert tag not in result.text
    # No relative URI may survive into the output.
    assert "part0.ts" not in result.text
    assert "part1.ts" not in result.text


def test_parse_marks_ads_rather_than_dropping_them():
    parsed = parse_media_playlist(MEDIA_WITH_STITCHED_ADS, BASE)
    assert len(parsed.segments) == 6
    assert parsed.ad_segment_count == 3
    assert [s.ad_source for s in parsed.segments if s.is_ad] == ["daterange"] * 3


def test_master_playlist_detection_and_variant_rewriting():
    assert is_master_playlist(MASTER)
    assert not is_master_playlist(CLEAN_MEDIA)

    result = rewrite_master(MASTER, BASE, rewrite_uri=lambda u: f"proxy::{u}")
    assert len(result.variants) == 2
    assert result.is_master
    assert "proxy::https://video-weaver.example.hls.ttvnw.net/v1/playlist/chunked/index.m3u8" in (
        result.text
    )
    # EXT-X-MEDIA URI attributes are rewritten too.
    assert 'URI="proxy::' in result.text
