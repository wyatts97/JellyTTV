"""The resolver decides whether Twitch stitches ads in at all.

Everything downstream (hls.py, stream_session.py) can only react to ads that
are already in the playlist: cut them and leave a hole, or pass them through and
show a commercial. Getting the access-token parameters right here is what stops
that dilemma from arising, so it is worth pinning down.
"""

from __future__ import annotations

import pytest

from app.services.resolver import (
    DEFAULT_PLAYER_TYPE,
    DEFAULT_PROXY_URL,
    PLAYER_TYPE_NONE,
    _streamlink_cmd,
    configured_proxy,
    live_cache_key,
    proxy_for,
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


# ------------------------------------------------------------------ ad proxy
def test_the_ad_avoidance_proxy_is_on_by_default():
    """Twitch decides whether to stitch an ad when it mints the token, so where
    that request comes from is the one lever that prevents ads existing."""
    cmd = _streamlink_cmd(URL, "best", None, None, configured_proxy(None))
    assert _param_value(cmd, "--http-proxy") == DEFAULT_PROXY_URL


def test_an_explicitly_cleared_proxy_setting_turns_it_off():
    """Empty and unset must stay distinguishable.

    `None` is "never configured" - which is what an upgraded database reads back
    - and has to select the default. If an empty string meant the same thing
    there would be no way to switch the proxy off that survived a restart.
    """
    assert configured_proxy(None) == DEFAULT_PROXY_URL
    assert configured_proxy("") is None
    assert "--http-proxy" not in _streamlink_cmd(URL, "best", None, None, configured_proxy(""))


def test_a_bare_host_port_gets_a_scheme():
    cmd = _streamlink_cmd(URL, "best", None, None, configured_proxy("10.0.0.5:8888"))
    assert _param_value(cmd, "--http-proxy") == "http://10.0.0.5:8888"


def test_an_authenticated_resolve_is_never_proxied():
    """The credential must not reach a third party. This is the whole rule.

    A Turbo or subscribed token already yields an ad-free playlist straight from
    Twitch, so proxying it would hand an OAuth token to a volunteer-run server
    in exchange for nothing.
    """
    assert proxy_for("tok123", DEFAULT_PROXY_URL) is None

    cmd = _streamlink_cmd(URL, "best", "tok123", None, DEFAULT_PROXY_URL)
    assert "--http-proxy" not in cmd
    assert "tok123" not in " ".join(
        part for part in cmd if not part.startswith("Authorization=")
    ), "the token leaked outside the auth header"
    assert _param_value(cmd, "--twitch-api-header") == "Authorization=OAuth tok123"


def test_cache_key_separates_proxied_from_direct_resolves():
    """One carries ads and the other does not; they cannot share an entry."""
    assert live_cache_key("chan", "best", None, DEFAULT_PROXY_URL) != live_cache_key(
        "chan", "best", None, None
    )


async def test_a_dead_proxy_falls_back_to_a_direct_resolve(monkeypatch):
    """Third-party infrastructure must never be able to take a stream down.

    The default proxy is community-run and maintained for someone else's users:
    it can vanish or start refusing non-browser clients at any time. Ads are
    worth avoiding; a dead channel is not worth trading for them.
    """
    from app.services import resolver

    monkeypatch.setattr(resolver.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[bool] = []

    async def fake_run(cmd, timeout=45.0):
        proxied = "--http-proxy" in cmd
        seen.append(proxied)
        if proxied:
            return 1, "", "Unable to open URL: proxy connection failed"
        return 0, "https://video-weaver.example.hls.ttvnw.net/v1/playlist/x.m3u8", ""

    monkeypatch.setattr(resolver, "_run", fake_run)

    url = await resolver._resolve(URL, "best", None, None, DEFAULT_PROXY_URL)

    assert url.startswith("https://video-weaver.")
    assert seen == [True, False], "expected a proxied attempt, then a direct one"


async def test_an_offline_channel_is_not_retried_without_the_proxy(monkeypatch):
    """Offline is a fact about the channel, not the route.

    Retrying directly would spawn streamlink a second time to learn the same
    thing, on every poll of every offline channel.
    """
    from app.services import resolver

    monkeypatch.setattr(resolver.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = {"n": 0}

    async def fake_run(cmd, timeout=45.0):
        calls["n"] += 1
        return 1, "", "No playable streams found on this URL"

    monkeypatch.setattr(resolver, "_run", fake_run)

    with pytest.raises(resolver.ChannelOffline):
        await resolver._resolve(URL, "best", None, None, DEFAULT_PROXY_URL)
    assert calls["n"] == 1


def test_cache_key_separates_player_types():
    """An ad-free and an ad-bearing url are different urls; never share an entry."""
    assert live_cache_key("chan", "best", "embed") != live_cache_key("chan", "best", "web")
    assert live_cache_key("chan", "best", None) == live_cache_key(
        "chan", "best", DEFAULT_PLAYER_TYPE
    )
