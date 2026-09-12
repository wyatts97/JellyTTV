"""Ad-free source mode: serve the one player type Twitch never stitches.

Measured live against three channels over seven ad breaks: every player type
offering more than 360p - `web`, `embed`, `popout`, `mobile_web`, `site` - is
stitched at the same moment the native stream is, and so is `autoplay`. Only
`picture-by-picture` stays clean, capped at 360p.

So there is no ad-free 1080p source to switch to, and covering a break by
switching always means a mid-timeline resolution change that ffmpeg's HLS
demuxer cannot absorb (it ignores #EXT-X-DISCONTINUITY, trac #5419). This mode
sidesteps the whole problem by never switching: play the clean source from the
start and there is no break to cover, nothing to hold on, and no seam.

These tests pin the three decisions that make up the mode.
"""

from __future__ import annotations

import pytest

from app.models import Settings
from app.routers import hls as hls_router
from app.services import resolver
from app.services.settings_store import ResolvedSettings


def make_settings(**kwargs) -> ResolvedSettings:
    return ResolvedSettings(row=Settings(**kwargs))


async def test_the_stream_is_resolved_from_the_ad_free_player_type():
    """The mode is nothing more than this: ask Twitch for the clean one."""
    seen: dict[str, object] = {}

    async def fake_resolve_live(login: str, **kwargs):
        seen.update(kwargs)
        seen["login"] = login
        return "https://video-weaver.test.hls.ttvnw.net/x.m3u8"

    settings = make_settings(ad_free_source=True, twitch_player_type="embed")
    original = resolver.resolve_live
    resolver.resolve_live = fake_resolve_live
    try:
        await hls_router._make_resolver("adapt", "best", settings)()
    finally:
        resolver.resolve_live = original

    assert seen["player_type"] == resolver.AD_FREE_PLAYER_TYPE
    assert seen["player_type"] == "picture-by-picture"


async def test_the_configured_player_type_is_used_when_the_mode_is_off():
    """The override must not leak into normal operation."""
    seen: dict[str, object] = {}

    async def fake_resolve_live(login: str, **kwargs):
        seen.update(kwargs)
        return "https://video-weaver.test.hls.ttvnw.net/x.m3u8"

    settings = make_settings(ad_free_source=False, twitch_player_type="embed")
    original = resolver.resolve_live
    resolver.resolve_live = fake_resolve_live
    try:
        await hls_router._make_resolver("adapt", "best", settings)()
    finally:
        resolver.resolve_live = original

    assert seen["player_type"] == "embed"


async def test_no_backup_search_runs_in_ad_free_mode():
    """Nothing to escape from, and nowhere to escape to.

    Leaving the rotation wired would spend a streamlink spawn per poll walking
    player types that are provably carrying the same pod, and then trip
    EXHAUSTED_COOLDOWN - which is the black screen this mode exists to remove.
    """
    assert hls_router._make_backup_finder(
        "adapt", make_settings(ad_free_source=True, strip_ads=True)
    ) is None

    # Still wired when the mode is off and blocking is on.
    assert hls_router._make_backup_finder(
        "adapt", make_settings(ad_free_source=False, strip_ads=True)
    ) is not None


@pytest.mark.parametrize("channel_quality", ["1080p60", "720p", None])
async def test_quality_requests_are_capped_to_what_the_clean_source_offers(
    channel_quality, monkeypatch
):
    """A channel pinned to 1080p60 would make streamlink fail outright.

    `picture-by-picture` publishes audio_only/160p/360p and nothing else, so a
    rendition request it cannot satisfy is not a downgrade - it is a resolve
    error and a dead channel. "best" asks for the top of whatever ladder this
    player type does offer.
    """

    class FakeChannel:
        enabled = True
        live_enabled = True
        display_name = "Adapt"
        quality = channel_quality

    async def fake_get(_session, _login):
        return FakeChannel()

    monkeypatch.setattr(hls_router.channel_service, "get_channel_by_login", fake_get)

    settings = make_settings(ad_free_source=True, default_quality="1080p60")
    assert await hls_router._channel_quality(None, settings, "adapt") == "best"


async def test_quality_overrides_still_apply_when_the_mode_is_off(monkeypatch):
    class FakeChannel:
        enabled = True
        live_enabled = True
        display_name = "Adapt"
        quality = "1080p60"

    async def fake_get(_session, _login):
        return FakeChannel()

    monkeypatch.setattr(hls_router.channel_service, "get_channel_by_login", fake_get)

    settings = make_settings(ad_free_source=False, default_quality="720p")
    assert await hls_router._channel_quality(None, settings, "adapt") == "1080p60"
