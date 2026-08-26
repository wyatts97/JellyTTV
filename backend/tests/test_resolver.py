"""The resolver decides whether Twitch stitches ads in at all.

Everything downstream (hls.py, stream_session.py) can only react to ads that
are already in the playlist: cut them and leave a hole, or pass them through and
show a commercial. Getting the access-token parameters right here is what stops
that dilemma from arising, so it is worth pinning down.
"""

from __future__ import annotations

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
