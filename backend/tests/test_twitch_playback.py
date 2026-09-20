"""Playback tokens minted straight from Twitch.

The module that replaces a streamlink spawn with two HTTP requests. What is
pinned here is the shape of those requests - a wrong `platform` or a stale
persisted hash fails silently into the streamlink fallback - and the rendition
choice, which is what keeps an ad break from changing the picture.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services import twitch_playback as tp

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="weaver.test",SERVER-TIME="1789922897.32"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="360p30",NAME="360p",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=630000,RESOLUTION=640x360,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="360p30",FRAME-RATE=30.000
https://usher.test/360p30.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p60",FRAME-RATE=60.000
https://usher.test/720p60.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="audio_only",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://usher.test/audio.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=6899515,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
https://usher.test/chunked.m3u8
"""

TOKEN_RESPONSE = {
    "data": {"streamPlaybackAccessToken": {"signature": "sig-abc", "value": "token-xyz"}}
}


@pytest.fixture(autouse=True)
def _clean_cache():
    tp.invalidate()
    yield
    tp.invalidate()


def mock_twitch(mock: respx.MockRouter, *, master: str = MASTER) -> None:
    mock.post(tp.GQL_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    mock.get(url__startswith="https://usher.ttvnw.net/api/channel/hls/").mock(
        return_value=httpx.Response(200, text=master)
    )


# -------------------------------------------------------------- the parser
def test_the_master_playlist_is_parsed_into_renditions():
    variants = tp.parse_master(MASTER)
    assert [v.quality for v in variants] == ["360p", "720p60", "audio_only", "best"]

    source = variants[-1]
    assert (source.width, source.height, source.frame_rate) == (1920, 1080, 60.0)
    assert source.group_id == "chunked"
    # The source group is spelled `best`, matching streamlink, rather than
    # carrying its "(source)" suffix through to the UI.
    assert source.quality == "best"
    assert variants[2].is_video is False, "audio_only is not a picture"


# ------------------------------------------------------- rendition choice
def test_the_matching_rendition_wins_over_a_bigger_one():
    """A covered ad break must not change the picture."""
    variants = tp.parse_master(MASTER)
    playing = tp.Variant(
        url="https://native.test/720.m3u8",
        group_id="720p60",
        name="720p60",
        bandwidth=3_000_000,
        codecs="avc1",
        width=1280,
        height=720,
        frame_rate=60.0,
    )
    chosen = tp.pick_variant(variants, match=playing)
    assert chosen is not None
    assert chosen.matches(playing)
    assert chosen.url == "https://usher.test/720p60.m3u8"


def test_the_closest_rendition_is_taken_when_nothing_matches():
    """`picture-by-picture` tops out at 360p; a break covered small beats black."""
    small = [v for v in tp.parse_master(MASTER) if v.height == 360]
    playing = tp.Variant(
        url="https://native.test/1080.m3u8",
        group_id="chunked",
        name="1080p60",
        bandwidth=7_000_000,
        codecs="avc1",
        width=1920,
        height=1080,
        frame_rate=60.0,
    )
    chosen = tp.pick_variant(small, match=playing)
    assert chosen is not None and chosen.height == 360
    assert not chosen.matches(playing)


def test_a_frame_rate_difference_is_not_a_match():
    """1080p30 and 1080p60 are different pictures to a decoder mid-stream."""
    thirty = tp.Variant(
        url="https://usher.test/1080p30.m3u8",
        group_id="1080p30",
        name="1080p30",
        bandwidth=5_000_000,
        codecs="avc1",
        width=1920,
        height=1080,
        frame_rate=30.0,
    )
    sixty = tp.Variant(
        url="https://native.test/1080p60.m3u8",
        group_id="chunked",
        name="1080p60",
        bandwidth=7_000_000,
        codecs="avc1",
        width=1920,
        height=1080,
        frame_rate=60.0,
    )
    assert not thirty.matches(sixty)


def test_quality_selection_without_anything_to_match():
    variants = tp.parse_master(MASTER)
    assert tp.pick_variant(variants, quality="best").height == 1080
    assert tp.pick_variant(variants, quality="worst").height == 360
    assert tp.pick_variant(variants, quality="720p60").height == 720
    # A rendition this ladder does not carry: the tallest at or below it.
    assert tp.pick_variant(variants, quality="480p").height == 360
    assert tp.pick_variant(variants, quality="audio_only").is_video is False


def test_a_quality_taller_than_the_whole_ladder_still_resolves():
    """Asking `picture-by-picture` for 1080p must land somewhere, not fail."""
    small = [v for v in tp.parse_master(MASTER) if v.height == 360]
    assert tp.pick_variant(small, quality="1080p60").height == 360


# ------------------------------------------------------------- the requests
@respx.mock
async def test_the_token_request_carries_what_twitch_expects():
    route = respx.post(tp.GQL_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(url__startswith="https://usher.ttvnw.net/").mock(
        return_value=httpx.Response(200, text=MASTER)
    )

    await tp.fetch_master("adapt", "embed", user_token="oauth-token", device_id="dev123")

    request = route.calls.last.request
    body = httpx.Response(200, content=request.content).json()
    assert body["extensions"]["persistedQuery"]["sha256Hash"] == tp.PLAYBACK_ACCESS_TOKEN_HASH
    assert body["variables"]["login"] == "adapt"
    assert body["variables"]["playerType"] == "embed"
    assert body["variables"]["platform"] == "web"
    assert request.headers["Client-ID"] == tp.CLIENT_ID
    # The same device id the ad-event report sends, so both describe one viewer.
    assert request.headers["X-Device-Id"] == "dev123"
    assert request.headers["Authorization"] == "OAuth oauth-token"


@respx.mock
async def test_autoplay_is_minted_for_android():
    """`autoplay` only exists on the android platform; asking as web is a dead end."""
    route = respx.post(tp.GQL_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    respx.get(url__startswith="https://usher.ttvnw.net/").mock(
        return_value=httpx.Response(200, text=MASTER)
    )

    await tp.fetch_master("adapt", "autoplay")
    body = httpx.Response(200, content=route.calls.last.request.content).json()
    assert body["variables"]["platform"] == "android"


@respx.mock
async def test_the_usher_request_carries_the_token_and_asks_for_h264_only():
    respx.post(tp.GQL_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    route = respx.get(url__startswith="https://usher.ttvnw.net/").mock(
        return_value=httpx.Response(200, text=MASTER)
    )

    await tp.fetch_master("adapt", "popout")

    url = route.calls.last.request.url
    assert url.path == "/api/channel/hls/adapt.m3u8"
    assert url.params["sig"] == "sig-abc"
    assert url.params["token"] == "token-xyz"
    # An HEVC rendition on one side of a switch and not the other is a codec
    # change mid-stream, which is the thing this module exists to avoid.
    assert url.params["supported_codecs"] == "avc1"


@respx.mock
async def test_an_offline_channel_is_reported_as_offline_not_as_an_error():
    """Twitch answers a token request for an offline channel with a null."""
    respx.post(tp.GQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"streamPlaybackAccessToken": None}})
    )
    with pytest.raises(tp.ChannelOffline):
        await tp.fetch_master("adapt", "embed")


@respx.mock
async def test_a_rotated_persisted_hash_is_an_ordinary_error():
    """It must raise rather than return nonsense: the caller falls back to streamlink."""
    respx.post(tp.GQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "PersistedQueryNotFound"}]})
    )
    with pytest.raises(tp.PlaybackError):
        await tp.fetch_master("adapt", "embed")


# ------------------------------------------------------------------ cache
@respx.mock
async def test_a_master_is_cached_per_channel_and_player_type():
    """The cache is what makes the first ad break as fast as the fifth."""
    mock_twitch(respx.mock)

    await tp.master("adapt", "embed")
    await tp.master("adapt", "embed")
    assert respx.calls.call_count == 2, "the second lookup re-minted a token"

    await tp.master("adapt", "popout")
    assert respx.calls.call_count == 4, "another player type needs its own token"


@respx.mock
async def test_forcing_and_invalidating_both_re_mint():
    mock_twitch(respx.mock)

    await tp.master("adapt", "embed")
    await tp.master("adapt", "embed", force=True)
    assert respx.calls.call_count == 4

    tp.invalidate("adapt", "embed")
    await tp.master("adapt", "embed")
    assert respx.calls.call_count == 6


@respx.mock
async def test_a_variant_url_can_be_traced_back_to_its_rendition():
    """How the backup search knows which picture the session is serving."""
    mock_twitch(respx.mock)
    await tp.master("adapt", "embed")

    found = tp.variant_for_url("https://usher.test/chunked.m3u8")
    assert found is not None
    assert (found.width, found.height) == (1920, 1080)
    # A streamlink-resolved url is not in the memory, and the caller then has
    # nothing to match against.
    assert tp.variant_for_url("https://video-weaver.test/x.m3u8") is None
