"""The resolver decides whether Twitch stitches ads in at all.

Everything downstream (hls.py, stream_session.py) can only react to ads that
are already in the playlist - the best it can do is find a clean copy of the
same stream on another player type and splice that over the break. Getting the
access-token parameters right here is what stops that from being necessary, so
it is worth pinning down.
"""

from __future__ import annotations

import pytest

from app.services import resolver
from app.services.resolver import (
    DEFAULT_PLAYER_TYPE,
    PLAYER_TYPE_NONE,
    _streamlink_cmd,
    live_cache_key,
    resolve_player_type,
)

URL = "https://www.twitch.tv/somechannel"


def _param_value(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_no_player_type_override_is_sent_by_default():
    """Overriding costs quality and does not stop ads, so we must not do it.

    streamlink's Twitch docs say ads get stitched in whichever player type is
    requested, and warn that a non-default one can be denied the highest quality
    renditions. Sending an override by default would trade resolution for a
    benefit that is not there.
    """
    cmd = _streamlink_cmd(URL, "best", None)
    assert "--twitch-access-token-param" not in cmd
    assert DEFAULT_PLAYER_TYPE == PLAYER_TYPE_NONE


def test_an_explicit_override_is_still_sent():
    """The knob has to keep working - this is undocumented, changing behaviour."""
    cmd = _streamlink_cmd(URL, "best", None, "frontpage")
    assert _param_value(cmd, "--twitch-access-token-param") == "playerType=frontpage"


def test_null_or_blank_player_type_falls_back_to_the_default():
    """Columns added by the additive migration read back NULL, not the default."""
    assert resolve_player_type(None) == DEFAULT_PLAYER_TYPE
    assert resolve_player_type("") == DEFAULT_PLAYER_TYPE
    assert resolve_player_type("  ") == DEFAULT_PLAYER_TYPE
    assert resolve_player_type("embed") == "embed"


def test_the_default_player_type_opts_out_of_the_override():
    """`web` is what Twitch assumes anyway; sending it explicitly buys nothing."""
    cmd = _streamlink_cmd(URL, "best", None, PLAYER_TYPE_NONE)
    assert "--twitch-access-token-param" not in cmd


def test_user_token_and_player_type_coexist():
    cmd = _streamlink_cmd(URL, "720p", "tok123", "embed")
    assert _param_value(cmd, "--twitch-access-token-param") == "playerType=embed"
    assert _param_value(cmd, "--twitch-api-header") == "Authorization=OAuth tok123"
    # The url and quality stay last - streamlink treats them positionally.
    assert cmd[-2:] == [URL, "720p"]


def test_cache_key_separates_player_types():
    """An ad-free and an ad-bearing url are different urls; never share an entry."""
    assert live_cache_key("chan", "best", "embed") != live_cache_key("chan", "best", "web")
    assert live_cache_key("chan", "best", None) == live_cache_key(
        "chan", "best", DEFAULT_PLAYER_TYPE
    )


async def test_a_resolve_survives_a_waiter_that_gave_up(monkeypatch):
    """A playlist deadline cancelling its wait must not cancel the resolve.

    streamlink's cold start regularly outlasts the render deadline; when the
    cancellation reached the subprocess, every retry started over and a slow
    channel never resolved at all. The retry must join the resolve in flight.
    """
    import asyncio

    calls = {"n": 0}
    release = asyncio.Event()

    async def slow_resolve(*_args, **_kwargs):
        calls["n"] += 1
        await release.wait()
        return "https://video-weaver.x.hls.ttvnw.net/v1/playlist/slow.m3u8"

    resolver.invalidate()
    monkeypatch.setattr(resolver, "_resolve", slow_resolve)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(resolver.resolve_live("slowpoke"), timeout=0.05)

    retry = asyncio.create_task(resolver.resolve_live("slowpoke", force=True))
    await asyncio.sleep(0)
    release.set()
    assert (await retry).endswith("slow.m3u8")
    assert calls["n"] == 1, "the retry started a second resolve instead of joining"
    # And the result was cached for the next caller.
    assert (await resolver.resolve_live("slowpoke")).endswith("slow.m3u8")
    assert calls["n"] == 1
    resolver.invalidate()


async def test_the_ad_free_source_never_falls_back_to_yt_dlp(monkeypatch):
    """yt-dlp cannot request a player type, so its answer carries the ads.

    Falling back to it for `picture-by-picture` quietly turned the ad-free
    source into the ad-stitched native stream.
    """
    ran: list[str] = []

    async def fake_run(cmd, *, timeout=0):
        ran.append(cmd[0])
        return 1, "", "error: some transient failure"

    monkeypatch.setattr(resolver.shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(resolver, "_run", fake_run)

    with pytest.raises(resolver.ResolveError):
        await resolver._resolve(URL, "best", None, player_type=resolver.AD_FREE_PLAYER_TYPE)
    assert ran == [resolver.STREAMLINK_BIN]

    ran.clear()
    with pytest.raises(resolver.ResolveError):
        await resolver._resolve(URL, "best", None, player_type=None)
    assert ran == [resolver.STREAMLINK_BIN, resolver.YTDLP_BIN]


def test_streamlink_stream_url_is_not_silenced():
    """`--quiet` makes streamlink 8 print nothing, not even the url."""
    cmd = _streamlink_cmd(URL, "best", None)
    assert "--quiet" not in cmd
    assert cmd[cmd.index("--loglevel") + 1] == "error"
