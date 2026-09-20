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


async def test_the_whole_rotation_runs_in_one_call(monkeypatch):
    """One call, every player type, concurrently.

    This used to be one candidate per poll, because each one cost a streamlink
    spawn of up to 8s. Minting tokens directly costs milliseconds, so a break is
    covered by the first poll that notices it instead of the fourth.
    """
    state = adblock.BackupState()
    minted = fake_masters(
        {
            "embed": [variant(1080, player_type="embed")],
            "popout": [variant(1080, player_type="popout")],
            "autoplay": [variant(360, 30.0, player_type="autoplay")],
            "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
        },
        monkeypatch,
    )

    async def fetch(url: str):
        # Only `popout` is out of the break.
        return 200, (CLEAN if "/popout/" in url else AD_MARKED)

    found = await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=state,
        fetch=fetch,
    )

    assert found is not None
    assert found.player_type == "popout"
    assert set(minted) == set(adblock.BACKUP_PLAYER_TYPES), "the rotation was not walked"
    assert "web" not in minted, "the native type must not be tried"
    # The contaminated types are cooling down.
    assert state.cooldowns.get("embed", 0) > 0


async def test_the_backup_matches_the_picture_being_served(monkeypatch):
    """A break must not change resolution.

    Switching to a 360p copy and back is two buffer re-initialisations in the
    player - the stutter this whole strategy exists to avoid - so a clean copy
    at the same resolution and frame rate wins over a clean copy at any other.
    """
    from app.services import twitch_playback

    native = variant(1080, player_type="native")
    monkeypatch.setattr(
        twitch_playback,
        "variant_for_url",
        lambda url: native if url == native.url else None,
    )
    fake_masters(
        {
            "embed": [
                variant(1080, player_type="embed"),
                variant(360, 30.0, player_type="embed"),
            ],
            "popout": [variant(360, 30.0, player_type="popout")],
            "autoplay": [variant(360, 30.0, player_type="autoplay")],
            "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
        },
        monkeypatch,
    )

    async def fetch(url: str):
        return 200, CLEAN  # every candidate is clean; only the picture differs

    found = await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=adblock.BackupState(),
        fetch=fetch,
        native_url=native.url,
    )

    assert found is not None
    assert found.player_type == "embed"
    # The 1080p rendition is the source group, which is spelled `best`.
    assert found.quality == "best"
    assert found.is_bridge is False, "a same-picture backup is not a bridge"


async def test_a_degraded_backup_beats_a_black_screen(monkeypatch):
    """When nothing matches, a smaller clean picture still beats a hold."""
    from app.services import twitch_playback

    native = variant(1080, player_type="native")
    monkeypatch.setattr(twitch_playback, "variant_for_url", lambda url: native)
    fake_masters(
        {
            "embed": [variant(1080, player_type="embed")],
            "popout": [variant(1080, player_type="popout")],
            "autoplay": [variant(360, 30.0, player_type="autoplay")],
            "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
        },
        monkeypatch,
    )

    async def fetch(url: str):
        # The full-ladder types carry the ad; only the preview tiers are clean.
        return 200, (AD_MARKED if ("/embed/" in url or "/popout/" in url) else CLEAN)

    found = await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=adblock.BackupState(),
        fetch=fetch,
        native_url=native.url,
    )

    assert found is not None
    assert found.player_type in {"autoplay", "picture-by-picture"}
    assert found.is_bridge is True, "a degraded backup must be marked for upgrading"


async def test_the_upgrade_probe_will_not_settle_for_another_degraded_rendition(
    monkeypatch,
):
    """Trading one low rendition for another buys a seam and no picture."""
    from app.services import twitch_playback

    native = variant(1080, player_type="native")
    monkeypatch.setattr(twitch_playback, "variant_for_url", lambda url: native)
    fake_masters(
        {
            "embed": [variant(720, player_type="embed")],
            "popout": [variant(480, 30.0, player_type="popout")],
            "autoplay": [variant(360, 30.0, player_type="autoplay")],
            "picture-by-picture": [variant(360, 30.0, player_type="picture-by-picture")],
        },
        monkeypatch,
    )

    async def fetch(url: str):
        return 200, CLEAN

    found = await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=adblock.BackupState(),
        fetch=fetch,
        native_url=native.url,
        full_quality_only=True,
    )

    assert found is None, "nothing matched the session picture, so nothing was promoted"


async def test_the_search_gives_up_when_every_type_carries_the_ad(monkeypatch):
    """Some breaks really are everywhere; the caller then holds."""
    state = adblock.BackupState()
    fake_masters(
        {pt: [variant(720, player_type=pt)] for pt in adblock.BACKUP_PLAYER_TYPES},
        monkeypatch,
    )

    async def fetch(url: str):
        return 200, AD_MARKED

    assert (
        await adblock.find_backup(
            login="chan",
            quality="best",
            native_player_type="web",
            state=state,
            fetch=fetch,
        )
        is None
    )

    # Having walked the whole rotation with nothing clean, it stops searching for
    # a moment rather than re-minting tokens on every poll of the break.
    assert state.exhausted_until > 0


async def test_a_dead_variant_url_invalidates_the_cached_master(monkeypatch):
    """A stale usher url is the master's fault, not the player type's.

    Penalising the type would remove a perfectly good backup from the rotation
    for the rest of the break; dropping the cached master makes the next attempt
    mint a fresh token instead.
    """
    from app.services import twitch_playback

    dropped: list[tuple] = []
    monkeypatch.setattr(
        twitch_playback, "invalidate", lambda *args, **kwargs: dropped.append(args)
    )
    fake_masters({"embed": [variant(720, player_type="embed")]}, monkeypatch)

    async def fetch(url: str):
        return 403, ""

    await adblock.find_backup(
        login="chan",
        quality="best",
        native_player_type="web",
        state=adblock.BackupState(),
        fetch=fetch,
    )
    assert dropped, "the cached master survived a dead variant url"
