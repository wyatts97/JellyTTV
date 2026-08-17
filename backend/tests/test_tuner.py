from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta

from app.models import Channel, SeasonScheme, VodMode
from app.services.tuner import build_m3u, build_xmltv
from app.util import utcnow


def _channel(login: str, *, live: bool, enabled: bool = True, live_enabled: bool = True) -> Channel:
    return Channel(
        id=abs(hash(login)) % 1000,
        twitch_login=login,
        twitch_user_id=str(abs(hash(login)) % 100000),
        display_name=login.title(),
        avatar_url=f"https://cdn.example/{login}.png",
        offline_image_url=f"https://cdn.example/{login}-offline.png",
        enabled=enabled,
        live_enabled=live_enabled,
        vod_mode=VodMode.strm,
        season_scheme=SeasonScheme.year,
        series_dir=login.title(),
        is_live=live,
        live_title=f"{login} is playing something" if live else None,
        live_game="Just Chatting" if live else None,
        live_viewers=4321 if live else None,
        live_started_at=utcnow() - timedelta(minutes=42) if live else None,
        live_thumbnail_url=(
            "https://cdn.example/preview-%{width}x%{height}.jpg" if live else None
        ),
    )


def test_m3u_contains_matching_tvg_ids_and_stream_urls():
    channels = [_channel("alpha", live=True), _channel("beta", live=False)]
    playlist = build_m3u(channels, base_url="http://jellyttv:8730", token="tok123")

    assert playlist.startswith("#EXTM3U")
    assert 'x-tvg-url="http://jellyttv:8730/tuner/guide.xml"' in playlist
    assert 'tvg-id="twitch.alpha"' in playlist
    assert 'tvg-logo="https://cdn.example/alpha.png"' in playlist
    assert 'group-title="Twitch"' in playlist
    assert "http://jellyttv:8730/hls/alpha/master.m3u8?key=tok123" in playlist
    # Offline channels are retained by default so Jellyfin channel ids stay stable.
    assert 'tvg-id="twitch.beta"' in playlist


def test_m3u_can_omit_offline_channels():
    channels = [_channel("alpha", live=True), _channel("beta", live=False)]
    playlist = build_m3u(
        channels, base_url="http://x:1", token=None, include_offline=False
    )
    assert "twitch.alpha" in playlist
    assert "twitch.beta" not in playlist


def test_m3u_skips_disabled_channels():
    channels = [
        _channel("alpha", live=True, enabled=False),
        _channel("beta", live=True, live_enabled=False),
        _channel("gamma", live=True),
    ]
    playlist = build_m3u(channels, base_url="http://x:1", token=None)
    assert "twitch.alpha" not in playlist
    assert "twitch.beta" not in playlist
    assert "twitch.gamma" in playlist


def test_m3u_omits_key_when_no_token():
    playlist = build_m3u([_channel("alpha", live=True)], base_url="http://x:1", token=None)
    assert "master.m3u8" in playlist
    assert "?key=" not in playlist


def test_xmltv_channel_ids_match_the_playlist():
    channels = [_channel("alpha", live=True), _channel("beta", live=False)]
    playlist = build_m3u(channels, base_url="http://x:1", token=None)
    guide = build_xmltv(channels, window_hours=12)

    root = ET.fromstring(guide.split("\n", 2)[2])
    guide_ids = {node.get("id") for node in root.findall("channel")}
    assert guide_ids == {"twitch.alpha", "twitch.beta"}
    for channel_id in guide_ids:
        assert f'tvg-id="{channel_id}"' in playlist


def test_xmltv_live_programme_carries_title_game_and_viewers():
    guide = build_xmltv([_channel("alpha", live=True)], window_hours=12)
    root = ET.fromstring(guide.split("\n", 2)[2])

    programmes = root.findall("programme")
    assert programmes, "expected at least one programme"
    first = programmes[0]
    assert first.get("channel") == "twitch.alpha"
    assert first.findtext("title") == "alpha is playing something"
    assert "Just Chatting" in (first.findtext("desc") or "")
    assert "4,321 viewers" in (first.findtext("desc") or "")
    assert first.find("live") is not None
    # Thumbnail placeholders must be expanded.
    icon = first.find("icon")
    assert icon is not None and "%{width}" not in (icon.get("src") or "")


def test_xmltv_fills_the_whole_window_even_when_offline():
    guide = build_xmltv([_channel("beta", live=False)], window_hours=12)
    root = ET.fromstring(guide.split("\n", 2)[2])
    programmes = root.findall("programme")
    assert len(programmes) >= 3
    assert all(p.findtext("title") == "Offline" for p in programmes)


def test_xmltv_timestamps_use_the_expected_format():
    guide = build_xmltv([_channel("alpha", live=True)], window_hours=6)
    root = ET.fromstring(guide.split("\n", 2)[2])
    start = root.find("programme").get("start")
    assert start is not None
    assert start.endswith(" +0000")
    assert len(start.split(" ")[0]) == 14
