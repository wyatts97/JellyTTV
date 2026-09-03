"""Cross-poll playlist continuity - the bugs that made streams stutter.

Every test here drives `stream_session.get_playlist` over a *series* of playlists
the way ffmpeg actually polls a live stream, because all of the defects being
pinned down are invisible in any single poll.
"""

from __future__ import annotations

import asyncio
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

    async def resolve() -> str:
        calls["n"] += 1
        return url

    return resolve, calls


HOLD_URI_PREFIX = "https://jellyttv.test/hls/adapt/hold?seq="


def hold_uri(seq: int) -> str:
    return f"{HOLD_URI_PREFIX}{seq}"


async def poll_all(
    playlists: list[str],
    *,
    strip_ads: bool = True,
    login: str = "adapt",
    backup=None,
    with_hold: bool = False,
):
    """Feed each playlist through the session in order, collecting the output.

    `with_hold` wires up the hold segment without a backup finder, which is the
    shape of a break that no player type could cover.
    """
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
            backup=backup,
            hold_uri=hold_uri if (with_hold or backup is not None) else None,
        )
        out.append(render)
        # Defeat the 1s render cache: each call must be treated as a fresh poll.
        session = stream_session.get(login, "best")
        session.last_render_at = 0.0
        # The backup search runs detached, so it needs a turn of the event loop
        # to finish. In production every poll awaits real network I/O and yields
        # many times over; these fakes never block, so yield explicitly.
        await asyncio.sleep(0)
    return out, resolve_calls


# ------------------------------------------------------------------ invariants
_MSEQ = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")
_SEGMENT_BLOCK = re.compile(
    r"(?P<disc>#EXT-X-DISCONTINUITY\n)?"
    r"(?:#EXT-X-PROGRAM-DATE-TIME:(?P<pdt>\S+)\n)?"
    r"#EXTINF:(?P<dur>[\d.]+),[^\n]*\n"
    r"(?P<uri>https://\S+)"
)
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

        assert_no_unmarked_hole(text)


def assert_no_unmarked_hole(text: str) -> None:
    """Wall-clock time and media time must advance together, or say why not.

    This is the invariant the desync bug lived under. Cutting content out of the
    window leaves `#EXT-X-PROGRAM-DATE-TIME` jumping further than the durations
    served, and because ffmpeg's HLS demuxer ignores `#EXT-X-DISCONTINUITY`
    (trac #5419) that jump reaches Jellyfin as raw timestamp movement, which
    video and audio absorb differently. Every previous assertion here passed
    throughout, because none of them looked at time.

    A discontinuity is the licence for the two clocks to disagree; without one
    they must match.
    """
    blocks = list(_SEGMENT_BLOCK.finditer(text))
    for previous, current in zip(blocks, blocks[1:], strict=False):
        if current.group("disc"):
            continue
        if not previous.group("pdt") or not current.group("pdt"):
            continue
        moved = (
            datetime.fromisoformat(current.group("pdt"))
            - datetime.fromisoformat(previous.group("pdt"))
        ).total_seconds()
        served = float(previous.group("dur"))
        assert abs(moved - served) < 0.5, (
            f"{moved:.3f}s of wall clock passed but {served:.3f}s was served, "
            f"with no discontinuity marking the gap"
        )


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


async def test_a_duration_less_pod_does_not_leak_its_tail_into_the_window():
    """A duration-less pod must not hand back its tail to keep the list full.

    The old parser un-marked the final segment so the playlist would never be
    empty, which put one segment of the commercial on screen every poll.

    A duration-less range runs to the end of the window by definition, so the
    content has to sit in front of it; once the window has scrolled entirely
    inside the range the poll is an all-ad pod and is passed through instead,
    which is a different rule tested separately.
    """
    playlists = [
        build_playlist(start_seq=100 + i, count=8, ad_at=104, ad_len=4, ad_duration=None)
        for i in range(3)
    ]
    renders, _ = await poll_all(playlists)
    for render in renders:
        assert not render.ad_pod, "content still precedes the pod in every poll"
        assert render.segment_count > 0
        for seq in range(104, 108):
            assert f"ad{seq}.ts" not in render.text, "an ad segment reached the player"
    assert "seg103.ts" in renders[-1].text, "content before the pod must survive"
    assert_playlist_continuity([r.text for r in renders])


async def test_a_full_ad_pod_is_held_over_never_shown():
    """An ad break shows a hold, not the ad, and never a hole.

    Three outcomes were possible here and two of them are wrong. Serving the ad
    shows the ad. Serving *nothing* removes the break's whole duration from the
    media timeline, and ffmpeg's HLS demuxer ignores `#EXT-X-DISCONTINUITY`
    (trac #5419), so the jump reaches Jellyfin uncompensated and leaves audio
    behind by the length of the break - permanently, and cumulatively across a
    session.

    The hold is the third option: our own black, silent, decodable second, so
    the timeline keeps advancing at real time with nothing missing from it. The
    picture waits instead of drifting, and no ad is ever committed.
    """
    # An 8-segment (16s) pod, so it spans two consecutive polls.
    playlists = [
        build_playlist(start_seq=100, count=4),  # real content
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),  # break ends, content resumes
    ]
    renders, _ = await poll_all(playlists, with_hold=True)

    # The pod is still recognised - it is covered deliberately, not missed.
    assert [r.ad_pod for r in renders] == [False, True, True, False]
    assert all(r.segment_count > 0 for r in renders), "the player must never starve"

    joined = "\n".join(r.text for r in renders)
    assert "ad104.ts" not in joined, "an ad segment reached the client"
    assert HOLD_URI_PREFIX in renders[1].text, "the break was not held"

    session = stream_session.get("adapt", "best")
    assert session.stats.hold_segments == 2, "one hold per ad poll"
    assert_playlist_continuity([r.text for r in renders])


async def test_a_hold_run_opens_one_discontinuity_not_one_per_segment():
    """DISCONTINUITY-SEQUENCE has to stay meaningful across a long break.

    A marker per hold segment would inflate the count as they scroll off, and a
    player that trusts it - which is the point of the field - would lose its
    place. The hold is one source, so it is one seam in and one seam out.
    """
    playlists = [build_playlist(start_seq=100, count=4)] + [
        build_playlist(start_seq=104 + i * 4, count=4, ad_at=104, ad_len=200, ad_duration=400.0)
        for i in range(6)
    ]
    renders, _ = await poll_all(playlists, with_hold=True)

    session = stream_session.get("adapt", "best")
    assert session.stats.hold_segments >= 6, "the break should have spanned many polls"

    holds = [u for u in _URI.findall(renders[-1].text) if u.startswith(HOLD_URI_PREFIX)]
    assert len(holds) == len(set(holds)), "a repeated hold uri would stall the playlist"

    # One marker for the seam into the hold. The window has long since rolled
    # past the native content, so nothing else in it is a new source.
    assert renders[-1].text.count("#EXT-X-DISCONTINUITY\n") <= 1
    assert_playlist_continuity([r.text for r in renders])


async def test_the_hold_ends_and_marks_the_seam_when_content_returns():
    """Leaving the hold is a source change like any other, and must be marked."""
    playlists = [
        build_playlist(start_seq=100, count=4),
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),
        build_playlist(start_seq=116, count=4),
    ]
    renders, _ = await poll_all(playlists, with_hold=True)

    session = stream_session.get("adapt", "best")
    assert session.holding is False, "the hold run never closed"

    # The window still holds native content from before the break, so what
    # matters is the segment immediately following the last hold - not the first
    # video-weaver uri in the playlist.
    blocks = list(_SEGMENT_BLOCK.finditer(renders[3].text))
    last_hold = max(
        i for i, b in enumerate(blocks) if b.group("uri").startswith(HOLD_URI_PREFIX)
    )
    assert last_hold + 1 < len(blocks), "content did not resume"
    resumed = blocks[last_hold + 1]
    assert resumed.group("uri").startswith("https://video-weaver")
    assert resumed.group("disc"), "the seam out of the hold was not marked"


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

    async def find_backup(state, quality, full_quality_only=False):
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
        # The backup search runs detached, so it needs a turn of the event loop
        # to finish. In production every poll awaits real network I/O and yields
        # many times over; these fakes never block, so yield explicitly.
        await asyncio.sleep(0)

    # The break costs exactly one poll of ad while the detached search completes -
    # the alternative, awaiting it inline, is what used to hold the response open
    # until ffmpeg gave up. Those segments then age out of the window normally,
    # so what matters is that no *further* ad is ever committed.
    ad_uris = {u for r in renders for u in _URI.findall(r.text) if "/ad1" in u}
    assert len(ad_uris) == 4, f"only the one pod-poll should have been served: {ad_uris}"

    # The first ad poll only *starts* the search and returns straight away - it
    # is never awaited inline, because resolving a backup means spawning
    # streamlink and doing that while the client waits for this playlist is what
    # made an ad break look like a dead channel.
    assert renders[1].backup_player_type is None

    # ...and from the next poll the break is filled with real video from the
    # backup, not silence.
    assert renders[2].backup_player_type == "embed"
    assert "bak200.ts" in renders[2].text
    assert renders[2].segment_count > 0, "the break produced no media"

    session = stream_session.get("adapt", "best")
    assert session.stats.backup_polls >= 2
    assert_playlist_continuity([r.text for r in renders])


async def test_native_resumes_only_after_several_clean_polls():
    """One clean poll is routinely the gap between two pods of one break."""
    native = [
        build_playlist(start_seq=100, count=4),
        # Two ad polls: the first starts the (detached) search, the second picks
        # its result up. A backup is never awaited inline.
        build_playlist(start_seq=104, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=108, count=4, ad_at=104, ad_len=8, ad_duration=16.0),
        build_playlist(start_seq=112, count=4),  # clean 1 - not yet
        build_playlist(start_seq=116, count=4),  # clean 2 - not yet
        build_playlist(start_seq=120, count=4),  # clean 3 - switch back
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

    async def find_backup(state, quality, full_quality_only=False):
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
        # See the note in the test above: let the detached search complete.
        await asyncio.sleep(0)

    assert serving == [False, False, True, True, True, False], (
        "expected the backup to be held across the first clean polls"
    )


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

    # The first poll is a cold session, which passes its pod through so the
    # player has something to probe; the heuristic bites from the second on.
    await poll()
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


async def test_ads_surrounded_by_content_are_still_stripped():
    """Passing a *pod* through must not amount to giving up on ad stripping.

    The distinction is whether removing the ads leaves a hole. Here they sit
    inside a window with real content on both sides, so cutting them costs
    nothing in continuity and they go - which is the case ad stripping was
    always for.
    """
    playlists = [
        build_playlist(start_seq=100 + i, count=8, ad_at=103, ad_len=2, ad_duration=4.0)
        for i in range(6)
    ]
    renders, _ = await poll_all(playlists)
    for render in renders:
        assert not render.ad_pod
        assert "ad103.ts" not in render.text
        assert "ad104.ts" not in render.text
    assert any(r.removed_segments > 0 for r in renders), "nothing was ever stripped"
    assert "seg102.ts" in renders[0].text and "seg105.ts" in renders[0].text
    assert_playlist_continuity([r.text for r in renders])


async def test_a_cold_session_passes_one_pod_rather_than_serving_nothing():
    """The one exception, and why it exists.

    ffmpeg cannot probe a media playlist with no segments, so serving headers
    only as the *first* thing a player ever sees fails the channel outright
    rather than waiting the pod out - which is why a channel with a preroll
    played in Streamyfin (iOS AVPlayer keeps polling and recovers) but never
    started in the Jellyfin web UI. `hls.rewrite_playlist` has always applied
    this rule on its own path: an unstripped ad is merely annoying, an empty
    playlist is fatal. Stripping resumes from the next poll, by which point
    there is a window to hold instead.
    """
    fetch, _ = make_fetch([build_playlist(start_seq=100, count=3, ad_at=100, ad_len=3, ad_duration=6.0)])
    resolve, _ = make_resolve()
    render = await stream_session.get_playlist(
        login="adapt", quality="best", strip_ads=True, resolve=resolve, fetch=fetch
    )
    assert render.ad_pod, "the pod is still recognised"
    assert render.segment_count == 3, "the player must have something to probe"


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


def test_the_continuity_guard_actually_catches_a_hole():
    """Proof the invariant above is not vacuous.

    A playlist where the clock jumps 20s while 2s of media is served, with
    nothing marking it - which is exactly what cutting an ad pod used to
    produce, and exactly what left audio 20s behind.
    """
    hole = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00\n"
        "#EXTINF:2.000,\n"
        "https://video-weaver.a.hls.ttvnw.net/v1/playlist/seg0.ts\n"
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:20+00:00\n"
        "#EXTINF:2.000,\n"
        "https://video-weaver.a.hls.ttvnw.net/v1/playlist/seg10.ts\n"
    )
    with pytest.raises(AssertionError, match="no discontinuity"):
        assert_no_unmarked_hole(hole)

    # The same jump is fine once it is declared, which is the only difference.
    declared = hole.replace(
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:20+00:00",
        "#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:20+00:00",
    )
    assert_no_unmarked_hole(declared)


async def test_target_duration_is_never_below_the_longest_segment():
    """Players size their buffer and reload interval off TARGETDURATION.

    Twitch nominally sends 2s segments but real EXTINF values drift above 2.5 on
    a keyframe shift, and `round` then declared 2 - understating the window, so
    players polled early and re-polled into an unchanged playlist.
    """
    stretched = build_playlist(start_seq=100, count=4).replace(
        "#EXTINF:2.000,", "#EXTINF:2.600,", 1
    )
    renders, _ = await poll_all([stretched])
    text = renders[0].text
    declared = int(re.search(r"#EXT-X-TARGETDURATION:(\d+)", text).group(1))
    longest = max(float(d) for d in re.findall(r"#EXTINF:([\d.]+),", text))
    assert declared >= longest, f"TARGETDURATION {declared} understates a {longest}s segment"


async def test_a_low_quality_bridge_is_upgraded_once_it_has_held():
    """The fast bridge buys coverage, not resolution - so it must not be final.

    `autoplay`/360p is reached for first because it is the quickest thing to come
    back clean, which is what covers a break on the first probe instead of the
    fourth. Nobody wants to watch a whole midroll at 360p, so once it has carried
    BRIDGE_HOLD_SECONDS a full-quality candidate is looked for behind it and
    swapped in if one exists.
    """
    native = [build_playlist(start_seq=100, count=4)] + [
        build_playlist(start_seq=104 + i * 4, count=4, ad_at=104, ad_len=200, ad_duration=400.0)
        for i in range(6)
    ]
    bridge_url = "https://video-weaver.b.hls.ttvnw.net/bridge.m3u8"
    full_url = "https://video-weaver.c.hls.ttvnw.net/full.m3u8"
    backup_seq = {"n": 200}
    native_fetch, _ = make_fetch(native)

    async def fetch(url: str):
        if url in (bridge_url, full_url):
            playlist = build_backup_playlist(start_seq=backup_seq["n"])
            backup_seq["n"] += 4
            return 200, playlist
        return await native_fetch(url)

    async def find_backup(state, quality, full_quality_only=False):
        if full_quality_only:
            return stream_session.BackupCandidate(
                player_type="embed", quality=quality, url=full_url, playlist="",
                is_bridge=False,
            )
        return stream_session.BackupCandidate(
            player_type="autoplay", quality="360p", url=bridge_url, playlist="",
            is_bridge=True,
        )

    resolve, _ = make_resolve()
    session = None
    for _ in native:
        await stream_session.get_playlist(
            login="adapt", quality="best", strip_ads=True,
            resolve=resolve, fetch=fetch, backup=find_backup, hold_uri=hold_uri,
        )
        session = stream_session.get("adapt", "best")
        session.last_render_at = 0.0
        # Age the bridge past its hold window so the upgrade probe can fire.
        if session.backup_promoted_at:
            session.backup_promoted_at -= stream_session.BRIDGE_HOLD_SECONDS
        await asyncio.sleep(0)

    assert session.stats.bridge_upgrades == 1, "the bridge was never traded up"
    assert session.backup.active.player_type == "embed"
    assert session.backup.active.is_bridge is False


async def test_the_bridge_upgrade_is_capped_per_break():
    """Every swap is another seam; past a couple they cost more than they buy."""
    native = [build_playlist(start_seq=100, count=4)] + [
        build_playlist(start_seq=104 + i * 4, count=4, ad_at=104, ad_len=400, ad_duration=800.0)
        for i in range(12)
    ]
    bridge_url = "https://video-weaver.b.hls.ttvnw.net/bridge.m3u8"
    backup_seq = {"n": 200}
    native_fetch, _ = make_fetch(native)

    async def fetch(url: str):
        if url == bridge_url:
            playlist = build_backup_playlist(start_seq=backup_seq["n"])
            backup_seq["n"] += 4
            return 200, playlist
        return await native_fetch(url)

    searches = {"upgrade": 0}

    async def find_backup(state, quality, full_quality_only=False):
        if full_quality_only:
            # Nothing better exists - the probe keeps coming back empty.
            searches["upgrade"] += 1
            return None
        return stream_session.BackupCandidate(
            player_type="autoplay", quality="360p", url=bridge_url, playlist="",
            is_bridge=True,
        )

    resolve, _ = make_resolve()
    session = None
    for _ in native:
        await stream_session.get_playlist(
            login="adapt", quality="best", strip_ads=True,
            resolve=resolve, fetch=fetch, backup=find_backup, hold_uri=hold_uri,
        )
        session = stream_session.get("adapt", "best")
        session.last_render_at = 0.0
        if session.backup_promoted_at:
            session.backup_promoted_at -= stream_session.BRIDGE_HOLD_SECONDS
        await asyncio.sleep(0)

    assert searches["upgrade"] == stream_session.MAX_BRIDGE_UPGRADES, (
        "the upgrade probe kept firing for the whole break"
    )
    assert session.serving_backup is True, "the bridge was dropped instead of held"
