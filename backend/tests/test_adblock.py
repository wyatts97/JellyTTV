"""Backup-stream ad avoidance.

The premise this whole strategy rests on: Twitch stitches ads *per token*, so
the same channel resolved for a different `playerType` is usually still carrying
the live content during a break. These tests pin down the acceptance rules that
decide whether a candidate is worth switching to, because switching to a bad one
trades an ad for a stall.
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


def test_strategy_falls_back_to_ttv_ab_for_null_or_nonsense():
    """NULL is what an upgraded database reads back for a new column."""
    assert adblock.configured_strategy(None) == adblock.STRATEGY_TTV_AB
    assert adblock.configured_strategy("") == adblock.STRATEGY_TTV_AB
    assert adblock.configured_strategy("nonsense") == adblock.STRATEGY_TTV_AB
    assert adblock.configured_strategy("ttv_lol_pro") == adblock.STRATEGY_TTV_LOL_PRO


async def test_the_search_tries_player_types_until_one_comes_back_clean():
    """The core of the strategy: one token is in a break, another is not."""
    state = adblock.BackupState()
    resolved: list[str] = []

    async def fake_fetch(url: str):
        # Only the third player type is out of the break.
        return 200, (CLEAN if url.endswith("mobile_web") else AD_MARKED)

    async def fake_resolve(login, *, quality, user_token, player_type, force):
        resolved.append(player_type)
        return f"https://video-weaver.b.hls.ttvnw.net/{player_type}"

    from app.services import resolver

    original = resolver.resolve_live
    resolver.resolve_live = fake_resolve
    try:
        found = await adblock.find_backup(
            login="chan",
            quality="best",
            native_player_type="site",
            state=state,
            fetch=fake_fetch,
        )
    finally:
        resolver.resolve_live = original

    assert found is not None
    assert found.player_type == "mobile_web"
    assert found.quality == "best", "should not have degraded quality unnecessarily"
    # The contaminated types it walked past are now cooling down.
    assert state.cooldowns.get("embed", 0) > 0
    assert "site" not in resolved, "the native type must not be tried"


async def test_the_search_gives_up_when_every_type_carries_the_ad():
    """Some breaks really are everywhere; the caller then serves nothing."""
    state = adblock.BackupState()

    async def fake_fetch(url: str):
        return 200, AD_MARKED

    async def fake_resolve(login, *, quality, user_token, player_type, force):
        return f"https://video-weaver.b.hls.ttvnw.net/{player_type}"

    from app.services import resolver

    original = resolver.resolve_live
    resolver.resolve_live = fake_resolve
    try:
        found = await adblock.find_backup(
            login="chan",
            quality="best",
            native_player_type="site",
            state=state,
            fetch=fake_fetch,
            allow_low_quality=False,
        )
    finally:
        resolver.resolve_live = original

    assert found is None
