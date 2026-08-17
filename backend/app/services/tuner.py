"""M3U playlist and XMLTV guide generation for Jellyfin's Live TV tuner.

Jellyfin setup (Dashboard -> Live TV):
  * Tuner Devices -> + -> "M3U Tuner"        -> <base>/tuner/playlist.m3u?key=<token>
  * TV Guide Data Providers -> + -> "XMLTV"  -> <base>/tuner/guide.xml?key=<token>

The `tvg-id` in the playlist matches the XMLTV `<channel id=...>` so Jellyfin
maps channels to guide data automatically.

Offline channels are kept in the playlist by default: Jellyfin keys channels by
id, and removing/re-adding them churns its database and loses user favourites.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import quote

from app.models import Channel
from app.util import twitch_thumbnail, utcnow, xmltv_time

OFFLINE_PROGRAMME_TITLE = "Offline"


def _stream_url(base_url: str, channel: Channel, token: str | None) -> str:
    url = f"{base_url.rstrip('/')}/hls/{quote(channel.twitch_login)}/master.m3u8"
    if token:
        url += f"?key={quote(token)}"
    return url


def build_m3u(
    channels: list[Channel],
    *,
    base_url: str,
    token: str | None,
    include_offline: bool = True,
) -> str:
    lines = ['#EXTM3U x-tvg-url="' + f"{base_url.rstrip('/')}/tuner/guide.xml" + '"']

    for channel in channels:
        if not channel.enabled or not channel.live_enabled:
            continue
        if not channel.is_live and not include_offline:
            continue

        attrs = [
            f'tvg-id="{channel.tvg_id}"',
            f'tvg-name="{channel.display_name}"',
            'group-title="Twitch"',
        ]
        if channel.avatar_url:
            attrs.append(f'tvg-logo="{channel.avatar_url}"')

        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{channel.display_name}")
        lines.append(_stream_url(base_url, channel, token))

    return "\n".join(lines) + "\n"


def build_xmltv(
    channels: list[Channel],
    *,
    window_hours: int = 48,
    include_offline: bool = True,
) -> str:
    now = utcnow().replace(second=0, microsecond=0)
    window_end = now + timedelta(hours=window_hours)

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "JellyTTV",
            "generator-info-url": "https://github.com/jellyttv/jellyttv",
            "source-info-name": "Twitch",
        },
    )

    for channel in channels:
        if not channel.enabled or not channel.live_enabled:
            continue
        if not channel.is_live and not include_offline:
            continue

        node = ET.SubElement(root, "channel", {"id": channel.tvg_id})
        ET.SubElement(node, "display-name").text = channel.display_name
        ET.SubElement(node, "display-name", {"lang": "en"}).text = channel.twitch_login
        if channel.avatar_url:
            ET.SubElement(node, "icon", {"src": channel.avatar_url})
        ET.SubElement(node, "url").text = f"https://www.twitch.tv/{channel.twitch_login}"

    for channel in channels:
        if not channel.enabled or not channel.live_enabled:
            continue
        if not channel.is_live and not include_offline:
            continue
        _append_programmes(root, channel, now, window_end)

    return '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n' + ET.tostring(
        root, encoding="unicode"
    )


def _append_programmes(root: ET.Element, channel: Channel, now, window_end) -> None:
    cursor = now - timedelta(hours=1)

    if channel.is_live:
        # A live broadcast has no known end time. Give it a rolling 4h block and
        # keep filling the rest of the window with placeholders so the Jellyfin
        # guide is never empty (an empty guide hides the channel in some clients).
        live_start = channel.live_started_at or now
        live_start = min(live_start, cursor)
        live_end = min(window_end, now + timedelta(hours=4))
        _programme(
            root,
            channel,
            start=live_start,
            stop=live_end,
            title=channel.live_title or f"{channel.display_name} live",
            description=_live_description(channel),
            category=channel.live_game,
            icon=twitch_thumbnail(channel.live_thumbnail_url),
            live=True,
        )
        cursor = live_end

    while cursor < window_end:
        stop = min(cursor + timedelta(hours=4), window_end)
        _programme(
            root,
            channel,
            start=cursor,
            stop=stop,
            title=OFFLINE_PROGRAMME_TITLE,
            description=f"{channel.display_name} is not streaming right now.",
            category=None,
            icon=channel.offline_image_url,
            live=False,
        )
        cursor = stop


def _live_description(channel: Channel) -> str:
    parts = [channel.live_title or ""]
    if channel.live_game:
        parts.append(f"Category: {channel.live_game}")
    if channel.live_viewers is not None:
        parts.append(f"{channel.live_viewers:,} viewers")
    parts.append(f"https://www.twitch.tv/{channel.twitch_login}")
    return "\n".join(p for p in parts if p)


def _programme(
    root: ET.Element,
    channel: Channel,
    *,
    start,
    stop,
    title: str,
    description: str,
    category: str | None,
    icon: str | None,
    live: bool,
) -> None:
    node = ET.SubElement(
        root,
        "programme",
        {"start": xmltv_time(start), "stop": xmltv_time(stop), "channel": channel.tvg_id},
    )
    ET.SubElement(node, "title", {"lang": "en"}).text = title
    ET.SubElement(node, "desc", {"lang": "en"}).text = description
    ET.SubElement(node, "category", {"lang": "en"}).text = category or "Twitch"
    if icon:
        ET.SubElement(node, "icon", {"src": icon})
    if live:
        ET.SubElement(node, "live")
        ET.SubElement(node, "new")
