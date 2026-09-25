"""Backup-stream ad avoidance.

The premise this whole strategy rests on: Twitch stitches ads *per token*, so
the same channel resolved for a different `playerType` is usually still carrying
the live content during a break. These tests pin down the acceptance rules that
decide whether a candidate is worth switching to, because switching to a bad one
trades an ad for a stall - and the two properties that decide whether the switch
is *invisible*: how fast it happens, and whether the picture changes.
"""

from __future__ import annotations

from app.services import adblock

CLEAN = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:2.000,live
https://video-weaver.b.hls.ttvnw.net/v1/playlist/seg100.ts
#EXTINF:2.000,live
https://video-weaver.b.hls.ttvnw.net/v1/playlist/seg101.ts
"""

AD_MARKED = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00.000Z"
#EXTINF:2.000,
https://video-weaver.b.hls.ttvnw.net/v1/playlist/ad0.ts
"""

CUE_OUT = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-CUE-OUT:DURATION=30.000
#EXTINF:2.000,
https://video-weaver.b.hls.ttvnw.net/v1/playlist/ad0.ts
"""

NO_MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
"""


def test_a_clean_playable_candidate_is_accepted():
    assert adblock.accepts(CLEAN) == (True, "clean-playable")


def test_an_ad_marked_candidate_is_rejected():
    """Switching to a backup that is itself mid-pod just moves the problem."""
    assert adblock.accepts(AD_MARKED) == (False, "ad-marked")


def test_a_generic_cue_out_candidate_is_rejected():
    """Twitch emits SCTE-style markers on some breaks, not only its daterange."""
    assert adblock.accepts(CUE_OUT) == (False, "ad-marked")


def test_a_candidate_with_no_media_is_rejected_before_it_is_checked_for_ads():
    """A playlist can resolve and parse while carrying nothing to play.

    Playability is tested first so the rejection reason is the useful one - it
    drives a much shorter cooldown than an ad-marked type gets.
    """
    assert adblock.accepts(NO_MEDIA) == (False, "not-playable")


def test_cooldowns_keep_a_contaminated_player_type_out_of_rotation():
    """An ad-marked type stays ad-marked for the length of the pod."""
    state = adblock.BackupState()
    now = 1000.0

    assert "embed" in state.available_types(exclude=None, now=now)
    state.penalise("embed", "ad-marked", now)

    assert "embed" not in state.available_types(exclude=None, now=now + 1)
    # ...but it comes back once the pod could plausibly be over.
    assert "embed" in state.available_types(
        exclude=None, now=now + adblock.COOLDOWNS["ad-marked"] + 1
    )


def test_the_native_player_type_is_never_offered_as_its_own_backup():
    """It is the one type known to be serving the ad we are escaping."""
    state = adblock.BackupState()
    assert "embed" not in state.available_types(exclude="embed", now=0.0)


def test_a_transport_error_is_forgiven_much_faster_than_an_ad():
    state = adblock.BackupState()
    assert adblock.COOLDOWNS["error"] < adblock.COOLDOWNS["ad-marked"]

    state.penalise("popout", "error", 0.0)
    assert "popout" in state.available_types(
        exclude=None, now=adblock.COOLDOWNS["error"] + 0.1
    )


def test_the_full_ladder_types_are_tried_before_the_preview_tiers():
    """Order is the difference between a seamless break and a visible one.

    `embed` and `popout` mint the whole rendition ladder, so they can cover a
    break at the picture already playing. `autoplay` and `picture-by-picture`
    are capped at 360p - worth having, but only once the others have failed.
    """
    order = adblock.BACKUP_PLAYER_TYPES
    assert order.index("embed") < order.index("picture-by-picture")
    assert order.index("popout") < order.index("autoplay")
    assert {"autoplay", "picture-by-picture"} == adblock.LOW_QUALITY_PLAYER_TYPES


# --------------------------------------------------------------- the search
def variant(height: int, fps: float = 60.0, *, player_type: str = "embed"):
    """A rendition as `twitch_playback` would hand one back."""
    from app.services.twitch_playback import Variant

    return Variant(
        url=f"https://usher.test/{player_type}/{height}p{int(fps)}.m3u8",
        group_id="chunked" if height >= 1080 else f"{height}p{int(fps)}",
        name=f"{height}p{int(fps)}",
        bandwidth=height * 6000,
        codecs="avc1.64002A,mp4a.40.2",
        width=round(height * 16 / 9),
        height=height,
        frame_rate=fps,
    )


def fake_masters(ladders: dict[str, list], monkeypatch) -> list[str]:
    """Serve a canned master playlist per player type. Returns the types minted."""
    from app.services import twitch_playback

    minted: list[str] = []

    async def fake_master(login, player_type, **kwargs):
        minted.append(player_type)
        if player_type not in ladders:
            raise twitch_playback.PlaybackError("no such player type")
        return twitch_playback.Master(
            login=login, player_type=player_type, variants=ladders[player_type]
        )

    monkeypatch.setattr(twitch_playback, "master", fake_master)
    return minted


async def refresh(state, fetch, **kwargs):
    return await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=state,
        fetch=fetch,
        **kwargs,
    )


def season(state) -> None:
    """Stand in for the time a warm token has been watched without a preroll."""
    for token in state.warm.values():
        token.born_at -= adblock.SEASON_SECONDS + 1


def full_ladders() -> dict[str, list]:
    return {
        "embed": [variant(1080, player_type="embed")],
        "popout": [variant(1080, player_type="popout")],
        "autoplay": [variant(360, 30.0, player_type="autoplay")],
        "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
    }


PREROLL_PLAYED_OUT = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-01-01T00:00:00.000Z",DURATION=4.0
#EXTINF:2.000,Amazon|2474283100494
https://video-weaver.b.hls.ttvnw.net/v1/playlist/ad0.ts
#EXTINF:2.000,Amazon|2474283100494
https://video-weaver.b.hls.ttvnw.net/v1/playlist/ad1.ts
#EXTINF:2.000,live
https://video-weaver.b.hls.ttvnw.net/v1/playlist/seg102.ts
#EXTINF:2.000,live
https://video-weaver.b.hls.ttvnw.net/v1/playlist/seg103.ts
"""


def test_a_token_whose_preroll_has_played_out_is_usable():
    """Its ad is still in the window; its live edge is content, which is what counts."""
    assert adblock.judge(PREROLL_PLAYED_OUT, "https://x/") == (True, False)
    assert adblock.judge(AD_MARKED, "https://x/") == (False, True)
    assert adblock.judge(CLEAN, "https://x/") == (True, False)


async def test_tokens_are_minted_once_and_then_only_watched(monkeypatch):
    """A fresh token is clean for one fetch and then gets a preroll.

    Minting on every search - the old design - meant every backup was a token
    about to be stitched. The pool mints each type once and after that only
    fetches its playlist, the way a viewer would, so the preroll plays out
    while nothing depends on it.
    """
    minted = fake_masters(full_ladders(), monkeypatch)

    async def fetch(url: str):
        return 200, CLEAN

    state = adblock.BackupState()
    # The first refresh only mints: a first fetch proves nothing.
    assert await refresh(state, fetch) is None
    for _ in range(3):
        await refresh(state, fetch)

    assert sorted(minted) == sorted(adblock.BACKUP_PLAYER_TYPES)
    assert "web" not in minted, "the native type must not be minted as its own backup"


async def test_a_token_that_has_had_its_preroll_beats_a_fresh_one(monkeypatch):
    """Even at a lower resolution: a fresh token will be stitched in seconds."""
    fake_masters(full_ladders(), monkeypatch)

    async def fetch(url: str):
        return 200, CLEAN

    state = adblock.BackupState()
    await refresh(state, fetch)
    state.warm["autoplay"].born_at -= adblock.SEASON_SECONDS + 1

    found = await refresh(state, fetch)
    assert found is not None
    assert found.player_type == "autoplay"
    assert found.seasoned is True

    # Once the full-quality tokens have proven themselves too, picture wins.
    season(state)
    found = await refresh(state, fetch)
    assert found.player_type == "embed"


async def test_a_token_is_seasoned_by_playing_out_its_preroll(monkeypatch):
    fake_masters({"embed": [variant(1080, player_type="embed")]}, monkeypatch)
    answers = iter([CLEAN, AD_MARKED, PREROLL_PLAYED_OUT])

    async def fetch(url: str):
        return 200, next(answers)

    state = adblock.BackupState()
    await refresh(state, fetch)  # mint
    first = await refresh(state, fetch)
    assert first is not None and first.seasoned is False, "clean on first fetch proves nothing"
    assert await refresh(state, fetch) is None, "a token carrying its preroll is not usable"
    found = await refresh(state, fetch)
    assert found is not None and found.seasoned is True
    assert state.stitches == {"embed": 1}


async def test_only_tokens_whose_live_edge_is_content_are_offered(monkeypatch):
    fake_masters(full_ladders(), monkeypatch)

    async def fetch(url: str):
        # Only `popout` is out of the break.
        return 200, (CLEAN if "/popout/" in url else AD_MARKED)

    state = adblock.BackupState()
    await refresh(state, fetch)
    season(state)
    found = await refresh(state, fetch)
    assert found is not None
    assert found.player_type == "popout"

    async def all_ads(url: str):
        return 200, AD_MARKED

    assert await refresh(state, all_ads) is None, "some breaks really are everywhere"


async def test_the_backup_matches_the_picture_being_served(monkeypatch):
    """A break should not change resolution when a same-picture token is clean."""
    from app.services import twitch_playback

    native = variant(1080, player_type="native")
    monkeypatch.setattr(
        twitch_playback,
        "variant_for_url",
        lambda url: native if url == native.url else None,
    )
    fake_masters(
        {
            "embed": [variant(1080, player_type="embed"), variant(360, 30.0, player_type="embed")],
            "popout": [variant(360, 30.0, player_type="popout")],
            "autoplay": [variant(360, 30.0, player_type="autoplay")],
            "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
        },
        monkeypatch,
    )

    async def fetch(url: str):
        return 200, CLEAN

    state = adblock.BackupState()
    await refresh(state, fetch, native_url=native.url)
    season(state)
    found = await refresh(state, fetch, native_url=native.url)

    assert found is not None
    assert found.player_type == "embed"
    assert found.quality == "best"
    assert found.is_bridge is False, "a same-picture backup is not a bridge"
    assert state.warm["autoplay"].is_bridge is True, "a degraded copy must be marked"


async def test_the_active_backup_is_never_offered_as_its_own_replacement(monkeypatch):
    fake_masters(full_ladders(), monkeypatch)

    async def fetch(url: str):
        return 200, CLEAN

    state = adblock.BackupState()
    await refresh(state, fetch)
    season(state)
    first = await refresh(state, fetch)
    state.active = first
    second = await refresh(state, fetch)
    assert second is not None and second.url != first.url


async def test_an_expired_token_is_dropped_and_re_minted(monkeypatch):
    """A dead variant url is the token's age, not the player type's fault."""
    from app.services import twitch_playback

    dropped: list[tuple] = []
    monkeypatch.setattr(
        twitch_playback, "invalidate", lambda *args, **kwargs: dropped.append(args)
    )
    minted = fake_masters({"embed": [variant(720, player_type="embed")]}, monkeypatch)

    async def dead(url: str):
        return 403, ""

    state = adblock.BackupState()
    await refresh(state, dead)
    for _ in range(adblock.MAX_WARM_FAILURES):
        await refresh(state, dead)
    assert dropped, "the cached master survived a dead variant url"
    assert "embed" not in state.warm

    state.cooldowns.clear()
    await refresh(state, dead)
    assert minted.count("embed") == 2
