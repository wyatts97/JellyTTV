"""Cross-poll playlist continuity - the bugs that made streams stutter.

Every test here drives `stream_session.get_playlist` over a *series* of playlists
the way ffmpeg actually polls a live stream, because all of the defects being
pinned down are invisible in any single poll.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.services import stream_session

pytestmark = pytest.mark.usefixtures("clean_sessions")


@pytest.fixture
def clean_sessions():
    stream_session.reset()
    yield
    stream_session.reset()


# ------------------------------------------------------------------- fixtures
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SEG_DURATION = 2.0


def build_playlist(
    *,
    start_seq: int,
    count: int = 6,
    host: str = "video-weaver.a.hls.ttvnw.net",
    ad_at: int | None = None,
    ad_len: int = 3,
    ad_duration: float | None = None,
    with_pdt: bool = True,
) -> str:
    """Render a Twitch-shaped media playlist window.

    `ad_at` is an absolute sequence number where a stitched-ad pod begins.
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{start_seq}",
    ]
    for i in range(count):
        seq = start_seq + i
        in_ad = ad_at is not None and ad_at <= seq < ad_at + ad_len
        if ad_at is not None and seq == ad_at:
            duration = f",DURATION={ad_duration}" if ad_duration is not None else ""
            lines.append(
                f'#EXT-X-DATERANGE:ID="stitched-ad-{ad_at}",CLASS="twitch-stitched-ad",'
                f'START-DATE="{(EPOCH + timedelta(seconds=seq * SEG_DURATION)).isoformat()}"'
                f"{duration}"
            )
        if with_pdt:
            stamp = (EPOCH + timedelta(seconds=seq * SEG_DURATION)).isoformat()
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
        lines.append(f"#EXTINF:{SEG_DURATION:.3f},")
        name = f"ad{seq}.ts" if in_ad else f"seg{seq}.ts"
        lines.append(f"https://{host}/v1/playlist/{name}")
    return "\n".join(lines) + "\n"


def make_fetch(playlists: list[str]):
    """A fetcher that walks a fixed list, then repeats the last entry."""
    calls = {"n": 0}

    async def fetch(url: str) -> tuple[int, str]:
        idx = min(calls["n"], len(playlists) - 1)
        calls["n"] += 1
        return 200, playlists[idx]

    return fetch, calls


def make_resolve(url: str = "https://video-weaver.a.hls.ttvnw.net/v1/playlist/x.m3u8"):
    calls = {"n": 0}

    async def resolve(prefer_direct: bool = False) -> str:
        calls["n"] += 1
        calls["prefer_direct"] = prefer_direct
        return url

    return resolve, calls


async def poll_all(playlists: list[str], *, strip_ads: bool = True, login: str = "adapt"):
    """Feed each playlist through the session in order, collecting the output."""
    fetch, _ = make_fetch(playlists)
    resolve, resolve_calls = make_resolve()
    out = []
    for _ in playlists:
        render = await stream_session.get_playlist(
            login=login,
            quality="best",
            strip_ads=strip_ads,
            resolve=resolve,
            fetch=fetch,
        )
        out.append(render)
        # Defeat the 1s render cache: each call must be treated as a fresh poll.
        session = stream_session.get(login, "best")
        session.last_render_at = 0.0
    return out, resolve_calls


# ------------------------------------------------------------------ invariants
_MSEQ = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")
_DSEQ = re.compile(r"#EXT-X-DISCONTINUITY-SEQUENCE:(\d+)")
_URI = re.compile(r"^https://\S+$", re.MULTILINE)


def assert_playlist_continuity(texts: list[str]) -> None:
    """The core HLS contract a live playlist must honour across polls."""
    last_mseq = -1
    last_dseq = 0
    last_target = 0
    seen_uri_seq: dict[str, int] = {}

    for text in texts:
        assert text.startswith("#EXTM3U")
        # A segment-less playlist is legal and expected during an ad pod: every
        # segment upstream was advertising, so there was nothing to serve. The
        # sequence invariants below still have to hold across that gap.
        uris = _URI.findall(text)

        mseq = int(_MSEQ.search(text).group(1))
        assert mseq >= last_mseq, "MEDIA-SEQUENCE moved backwards"
        last_mseq = mseq

        dseq_match = _DSEQ.search(text)
        dseq = int(dseq_match.group(1)) if dseq_match else 0
        assert dseq >= last_dseq, "DISCONTINUITY-SEQUENCE moved backwards"
        last_dseq = dseq

        target = int(re.search(r"#EXT-X-TARGETDURATION:(\d+)", text).group(1))
        assert target >= last_target, "TARGETDURATION shrank mid-session"
        last_target = target

        # No segment may ever be handed out under two different sequence numbers.
        for offset, uri in enumerate(uris):
            seq = mseq + offset
            if uri in seen_uri_seq:
                assert seen_uri_seq[uri] == seq, f"{uri} re-emitted under a new sequence number"
            else:
                seen_uri_seq[uri] = seq


# ----------------------------------------------------------------------- tests
async def test_media_sequence_is_monotonic_across_many_polls():
    playlists = [build_playlist(start_seq=100 + i) for i in range(50)]
    renders, _ = await poll_all(playlists)
    assert_playlist_continuity([r.text for r in renders])
    # Steady state: each poll advances the window by exactly one segment.
    assert renders[-1].media_sequence > renders[0].media_sequence


async def test_segments_are_never_re_emitted_under_a_new_sequence():
    playlists = [build_playlist(start_seq=100 + i) for i in range(20)]
    renders, _ = await poll_all(playlists)
    assert_playlist_continuity([r.text for r in renders])


async def test_upstream_sequence_numbering_is_not_copied_through():
    """The old proxy echoed upstream MEDIA-SEQUENCE while dropping segments."""
    playlists = [build_playlist(start_seq=900000 + i) for i in range(5)]
    renders, _ = await poll_all(playlists)
    assert renders[0].media_sequence == 0


async def test_lingering_ad_daterange_is_stripped_once_not_every_poll():
    """The ad DATERANGE stays in the window for several polls.

    Without cross-poll memory the same ad was re-classified each time and a
    fresh discontinuity re-inserted, so the output kept shifting under ffmpeg.
    """
    playlists = [
        build_playlist(start_seq=100 + i, ad_at=104, ad_len=3, ad_duration=6.0)
        for i in range(8)
    ]
    renders, _ = await poll_all(playlists)
    texts = [r.text for r in renders]
    assert_playlist_continuity(texts)

    for text in texts:
        assert "ad104.ts" not in text
        assert text.count("#EXT-X-DISCONTINUITY\n") <= 1


async def test_discontinuity_sequence_increments_when_one_scrolls_off():
    playlists = [
        build_playlist(start_seq=100 + i, ad_at=104, ad_len=3, ad_duration=6.0)
        for i in range(40)
    ]
    renders, _ = await poll_all(playlists)
    assert_playlist_continuity([r.text for r in renders])
    assert renders[-1].discontinuity_sequence >= 1, (
        "a discontinuity that has left the window must be counted (RFC 8216 6.2.1)"
    )


async def test_weaver_switch_is_absorbed_when_pdt_matches():
    """A new weaver host renames every segment; PDT keeps identity stable."""
    playlists = [build_playlist(start_seq=100 + i, host="video-weaver.a.hls.ttvnw.net") for i in range(5)]
    playlists += [
        build_playlist(start_seq=105 + i, host="video-weaver.b.hls.ttvnw.net") for i in range(5)
    ]
    renders, _ = await poll_all(playlists)
    assert_playlist_continuity([r.text for r in renders])


async def test_a_duration_less_pod_serves_no_segments_rather_than_leaking_one():
    """A duration-less pod must not hand back its tail to keep the list full.

    The old parser un-marked the final segment so the playlist would never be
    empty, which put one segment of the commercial on screen every poll. An
    all-ad window now renders headers only, exactly as the pre-session proxy
    did.
    """
    playlists = [
        build_playlist(start_seq=100 + i, count=4, ad_at=100 + i, ad_len=4, ad_duration=None)
        for i in range(6)
    ]
    renders, _ = await poll_all(playlists)
    for render in renders:
        assert render.ad_pod
        assert render.segment_count == 0
        assert "ad1" not in render.text, "an ad segment reached the player"
    assert_playlist_continuity([r.text for r in renders])


async def test_a_full_ad_pod_serves_nothing_and_content_resumes_after():
    """The regression that put full-quality ad breaks on screen.

    When every segment upstream is an ad, `kept` is empty. The old code read
    that as "we are about to serve an empty playlist" and passed the entire pod
    through, so the viewer got the ad in full quality - the single outcome ad
    stripping exists to prevent. Nothing is served now: the playlist goes to
    headers only for the length of the break, which is what the original
    stateless proxy did, and real content on both sides still flows normally.
    """
    # An 8-segment (16s) pod, so it spans two consecutive polls.
    playlists = [
        build_playlist(start_seq=100, count=4),  # real content
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),  # break ends, content resumes
    ]
    renders, _ = await poll_all(playlists)

    for render in renders:
        assert "ad10" not in render.text and "ad11" not in render.text, (
            "an ad segment reached the player"
        )

    # The break renders nothing at all rather than re-serving stale segments.
    assert [r.ad_pod for r in renders] == [False, True, True, False]
    assert renders[1].segment_count == 0

    # Real content from both sides of the break still gets through.
    assert "seg100.ts" in renders[0].text
    assert "seg112.ts" in renders[-1].text
    assert_playlist_continuity([r.text for r in renders])

    session = stream_session.get("adapt", "best")
    assert session.stats.ad_pod_polls == 2
    assert session.consecutive_ad_polls == 0, "the run must reset when content resumes"


def build_backup_playlist(*, start_seq: int, count: int = 4) -> str:
    """The same channel from another player type - clean, and still live."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{start_seq}",
    ]
    for i in range(count):
        seq = start_seq + i
        stamp = (EPOCH + timedelta(seconds=seq * SEG_DURATION)).isoformat()
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
        lines.append(f"#EXTINF:{SEG_DURATION:.3f},live")
        lines.append(f"https://video-weaver.b.hls.ttvnw.net/v1/playlist/bak{seq}.ts")
    return "\n".join(lines) + "\n"


async def test_an_ad_break_plays_a_backup_stream_instead_of_dead_air():
    """The whole point of the TTV-AB strategy.

    An ad is stitched per token, so the same channel on another player type is
    usually still carrying the live content. Splicing it in turns a break from
    dead air for its full duration into a seam. Every previous approach here
    accepted the hole because it assumed there was nothing else to play; there
    is.
    """
    native = [
        build_playlist(start_seq=100, count=4),
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),
        build_playlist(start_seq=116, count=4),
        build_playlist(start_seq=120, count=4),
    ]
    backup_url = "https://video-weaver.b.hls.ttvnw.net/backup.m3u8"
    backup_seq = {"n": 200}
    native_fetch, _ = make_fetch(native)

    async def fetch(url: str):
        if url == backup_url:
            playlist = build_backup_playlist(start_seq=backup_seq["n"])
            backup_seq["n"] += 4
            return 200, playlist
        return await native_fetch(url)

    async def find_backup(state, quality):
        return stream_session.BackupCandidate(
            player_type="embed",
            quality=quality,
            url=backup_url,
            playlist=build_backup_playlist(start_seq=200),
        )

    resolve, _ = make_resolve()
    renders = []
    for _ in native:
        renders.append(
            await stream_session.get_playlist(
                login="adapt",
                quality="best",
                strip_ads=True,
                resolve=resolve,
                fetch=fetch,
                backup=find_backup,
            )
        )
        stream_session.get("adapt", "best").last_render_at = 0.0

    # No ad segment ever reaches the player...
    for render in renders:
        assert "ad10" not in render.text and "ad11" not in render.text

    # ...and the break is filled with real video from the backup, not silence.
    assert renders[1].backup_player_type == "embed"
    assert "bak200.ts" in renders[1].text
    assert renders[1].segment_count > 0, "the break produced no media"

    session = stream_session.get("adapt", "best")
    assert session.stats.backup_polls >= 2
    assert_playlist_continuity([r.text for r in renders])


async def test_native_resumes_only_after_several_clean_polls():
    """One clean poll is routinely the gap between two pods of one break."""
    native = [
        build_playlist(start_seq=100, count=4),
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=4, ad_duration=8.0),
        build_playlist(start_seq=108, count=4),  # clean 1 - not yet
        build_playlist(start_seq=112, count=4),  # clean 2 - not yet
        build_playlist(start_seq=116, count=4),  # clean 3 - switch back
    ]
    backup_url = "https://video-weaver.b.hls.ttvnw.net/backup.m3u8"
    backup_seq = {"n": 200}
    native_fetch, _ = make_fetch(native)

    async def fetch(url: str):
        if url == backup_url:
            playlist = build_backup_playlist(start_seq=backup_seq["n"])
            backup_seq["n"] += 4
            return 200, playlist
        return await native_fetch(url)

    async def find_backup(state, quality):
        return stream_session.BackupCandidate(
            player_type="embed", quality=quality, url=backup_url, playlist=""
        )

    resolve, _ = make_resolve()
    serving = []
    for _ in native:
        await stream_session.get_playlist(
            login="adapt",
            quality="best",
            strip_ads=True,
            resolve=resolve,
            fetch=fetch,
            backup=find_backup,
        )
        session = stream_session.get("adapt", "best")
        session.last_render_at = 0.0
        serving.append(session.serving_backup)

    assert serving == [False, True, True, True, False], (
        "expected the backup to be held across the first clean polls"
    )


async def test_an_ad_pod_triggers_one_re_resolve_from_the_other_egress():
    """Go and get a different stream rather than waiting the break out.

    An ad is baked in when Twitch mints the playback token, so a fresh token
    from a different egress often comes back clean - which turns a break from
    dead air for its full duration into a brief gap. Flipping the proxied/direct
    polarity is what makes the retry meaningfully different, since the thing
    that changes the outcome is the IP Twitch sees.
    """
    playlists = [
        build_playlist(start_seq=100, count=4),
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),
    ]
    fetch, _ = make_fetch(playlists)
    resolve, resolve_calls = make_resolve()

    for _ in playlists:
        await stream_session.get_playlist(
            login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch
        )
        stream_session.get("adapt", "best").last_render_at = 0.0

    session = stream_session.get("adapt", "best")
    # Exactly one replacement for the whole break: retrying every poll would
    # respawn streamlink several times a second for the length of a midroll.
    assert session.stats.ad_replacements == 1
    assert session.prefer_direct_resolve is True, "polarity did not flip"
    assert resolve_calls["prefer_direct"] is True, "the resolver was not told to go direct"


async def test_the_polarity_flip_is_capped_not_repeated_every_poll():
    """A long break must not thrash streamlink once per poll."""
    playlists = [build_playlist(start_seq=100, count=4)] + [
        build_playlist(start_seq=104 + i * 4, count=4, ad_at=104, ad_len=200, ad_duration=400.0)
        for i in range(8)
    ]
    await poll_all(playlists)
    session = stream_session.get("adapt", "best")

    assert session.stats.ad_pod_polls >= 6, "the break should have spanned many polls"
    assert session.stats.ad_replacements == 1, "one replacement per break, not per poll"


def build_amazon_titled_playlist(*, start_seq: int, count: int = 4) -> str:
    """A stream whose *name* trips the ad-title heuristic.

    The title is contrived: since the bare `amazon` rule was removed, a real
    stream name no longer collides with the pattern. It stays as a stand-in for
    any future title that does, because the self-disabling guard it exercises is
    what keeps such a collision from making a channel permanently unplayable.
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{start_seq}",
    ]
    for i in range(count):
        seq = start_seq + i
        stamp = (EPOCH + timedelta(seconds=seq * SEG_DURATION)).isoformat()
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
        lines.append(f"#EXTINF:{SEG_DURATION:.3f},twitch-ad review stream")
        lines.append(f"https://video-weaver.a.hls.ttvnw.net/v1/playlist/seg{seq}.ts")
    return "\n".join(lines) + "\n"


async def test_a_misfiring_title_heuristic_stops_being_believed():
    """The safeguard that makes the deliberately loose title rule survivable.

    Segment titles carry the stream title, so a channel called "Amazon haul
    unboxing" has every segment classified as an ad and would never play at all.
    A real pod ends after a couple of minutes; a stream name does not - so an
    ad-only run that never ends, with no daterange ever corroborating it, means
    the heuristic is wrong rather than the break being long.
    """
    login = "amazonhaul"
    fetch, _ = make_fetch([build_amazon_titled_playlist(start_seq=100 + i * 4) for i in range(4)])
    resolve, _ = make_resolve()

    async def poll():
        render = await stream_session.get_playlist(
            login=login, quality="best", strip_ads=True, resolve=resolve, fetch=fetch
        )
        stream_session.get(login, "best").last_render_at = 0.0
        return render

    render = await poll()
    session = stream_session.get(login, "best")
    assert session.trust_titles, "the heuristic starts trusted"
    assert render.ad_pod, "the whole stream currently reads as advertising"

    # Fast-forward the run to the edge of what a real break could be.
    session.consecutive_ad_polls = stream_session.MAX_CONSECUTIVE_AD_POLLS - 1
    await poll()
    assert not session.trust_titles, "the heuristic should have been revoked"

    # The channel plays from here on rather than staying dark.
    before = session.next_seq
    render = await poll()
    assert not render.ad_pod
    assert session.next_seq > before, "playback did not resume after recovery"
    assert "seg" in render.text


async def test_a_corroborated_pod_never_blames_the_title_rule():
    """A genuine break must not disable detection, however long it runs."""
    playlists = [build_playlist(start_seq=100, count=4)] + [
        build_playlist(start_seq=104 + i * 4, count=4, ad_at=104, ad_len=200, ad_duration=400.0)
        for i in range(6)
    ]
    renders, _ = await poll_all(playlists)
    session = stream_session.get("adapt", "best")

    assert session.stats.ad_pod_polls >= 1, "the break should have suppressed output"
    assert session.trust_titles, "a daterange-backed pod must not blame the title rule"
    assert_playlist_continuity([r.text for r in renders])


async def test_an_all_ad_playlist_is_never_passed_through():
    """Not even on a cold session: an ad must never reach the player."""
    fetch, _ = make_fetch([build_playlist(start_seq=100, count=3, ad_at=100, ad_len=3, ad_duration=6.0)])
    resolve, _ = make_resolve()
    render = await stream_session.get_playlist(
        login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch
    )
    assert render.ad_pod
    assert render.segment_count == 0
    assert "ad100.ts" not in render.text


async def test_dead_upstream_status_triggers_a_reresolve():
    statuses = [403, 200]
    playlists = [None, build_playlist(start_seq=100)]
    calls = {"n": 0}

    async def fetch(url: str) -> tuple[int, str]:
        i = calls["n"]
        calls["n"] += 1
        return statuses[i], playlists[i] or ""

    resolve, resolve_calls = make_resolve()
    render = await stream_session.get_playlist(
        login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch
    )
    assert render.segment_count > 0
    assert resolve_calls["n"] == 2, "a 403 must force a fresh streamlink resolve"


async def test_upstream_is_resolved_once_for_many_polls():
    """The old 20s resolver TTL respawned streamlink constantly."""
    playlists = [build_playlist(start_seq=100 + i) for i in range(30)]
    _renders, resolve_calls = await poll_all(playlists)
    assert resolve_calls["n"] == 1


async def test_transient_failure_repeats_the_last_playlist_instead_of_erroring():
    playlists = [build_playlist(start_seq=100)]
    fetch_ok, _ = make_fetch(playlists)
    resolve, _ = make_resolve()
    first = await stream_session.get_playlist(
        login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch_ok
    )
    session = stream_session.get("adapt", "best")
    session.last_render_at = 0.0

    async def dead_fetch(url: str) -> tuple[int, str]:
        return 500, ""

    second = await stream_session.get_playlist(
        login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=dead_fetch
    )
    assert second.text == first.text
    assert second.from_cache


async def test_window_and_seen_map_stay_bounded():
    playlists = [build_playlist(start_seq=100 + i) for i in range(300)]
    await poll_all(playlists)
    session = stream_session.get("adapt", "best")
    assert len(session.window) <= stream_session.MAX_WINDOW
    assert len(session.seen) <= stream_session.SEEN_MAX


async def test_concurrent_polls_do_not_double_advance():
    import asyncio

    playlist = build_playlist(start_seq=100)
    fetch_calls = {"n": 0}

    async def fetch(url: str) -> tuple[int, str]:
        fetch_calls["n"] += 1
        return 200, playlist

    resolve, _ = make_resolve()
    results = await asyncio.gather(
        *[
            stream_session.get_playlist(
                login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch
            )
            for _ in range(5)
        ]
    )
    assert len({r.text for r in results}) == 1
    assert fetch_calls["n"] == 1, "the render cache must collapse a burst into one fetch"


async def test_idle_sessions_are_swept():
    playlists = [build_playlist(start_seq=100)]
    await poll_all(playlists)
    session = stream_session.get("adapt", "best")
    session.last_access -= stream_session.SESSION_IDLE_SECONDS + 1
    assert stream_session.sweep() == 1
    assert stream_session.get("adapt", "best") is None


async def test_segments_without_pdt_still_dedupe_by_filename():
    playlists = [build_playlist(start_seq=100 + i, with_pdt=False) for i in range(15)]
    renders, _ = await poll_all(playlists)
    assert_playlist_continuity([r.text for r in renders])
