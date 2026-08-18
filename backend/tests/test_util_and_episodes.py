from __future__ import annotations

from datetime import datetime

import pytest

from app.models import SeasonScheme
from app.services.episodes import (
    assign_numbers,
    compute_episode,
    compute_season,
    format_episode_tag,
)
from app.util import (
    normalise_channel_input,
    parse_twitch_duration,
    parse_twitch_time,
    sanitize_filename,
    twitch_thumbnail,
    xmltv_time,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("shroud", "shroud"),
        ("Shroud", "shroud"),
        ("https://www.twitch.tv/shroud", "shroud"),
        ("http://twitch.tv/shroud/videos", "shroud"),
        ("m.twitch.tv/shroud?foo=1", "shroud"),
        ("https://go.twitch.tv/shroud#anchor", "shroud"),
        ("  shroud  ", "shroud"),
        ("", ""),
    ],
)
def test_normalise_channel_input(raw: str, expected: str):
    assert normalise_channel_input(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3h21m33s", 3 * 3600 + 21 * 60 + 33),
        ("45m", 2700),
        ("12s", 12),
        ("2h", 7200),
        (None, None),
        ("nonsense", None),
    ],
)
def test_parse_twitch_duration(raw, expected):
    assert parse_twitch_duration(raw) == expected


def test_sanitize_filename_strips_illegal_characters():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a b c d e f g h i j"
    assert sanitize_filename("trailing dots...") == "trailing dots"
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("CON") == "_CON"
    assert len(sanitize_filename("x" * 500, max_length=40)) == 40


def test_twitch_thumbnail_placeholder_expansion():
    # VOD format: %{width} / %{height}
    url = "https://cdn/preview-%{width}x%{height}.jpg"
    assert twitch_thumbnail(url, 320, 180) == "https://cdn/preview-320x180.jpg"
    assert twitch_thumbnail(None) is None

    # Live stream format: {width} / {height}
    live_url = "https://cdn/live-{width}x{height}.jpg"
    assert twitch_thumbnail(live_url, 320, 180) == "https://cdn/live-320x180.jpg"

    # Mixed / already-substituted URL should be unchanged
    resolved = "https://cdn/preview-1280x720.jpg"
    assert twitch_thumbnail(resolved, 320, 180) == "https://cdn/preview-1280x720.jpg"


def test_parse_twitch_time_returns_naive_utc():
    parsed = parse_twitch_time("2026-03-04T05:06:07Z")
    assert parsed == datetime(2026, 3, 4, 5, 6, 7)
    assert parsed.tzinfo is None
    assert parse_twitch_time("garbage") is None


def test_xmltv_time_format():
    assert xmltv_time(datetime(2026, 3, 4, 5, 6, 7)) == "20260304050607 +0000"


# ------------------------------------------------------------------ numbering
def test_year_scheme_is_ordered_and_stable():
    jan_first = datetime(2026, 1, 1, 12, 0)
    dec_last = datetime(2026, 12, 31, 12, 0)

    assert compute_season(jan_first, SeasonScheme.year) == 2026
    assert compute_episode(jan_first, SeasonScheme.year, 0) == 10
    assert compute_episode(dec_last, SeasonScheme.year, 0) == 3650
    assert compute_episode(jan_first, SeasonScheme.year, 0) < compute_episode(
        dec_last, SeasonScheme.year, 0
    )


def test_calendar_month_scheme():
    day = datetime(2026, 7, 15, 8, 0)
    assert compute_season(day, SeasonScheme.calendar_month) == 202607
    assert compute_episode(day, SeasonScheme.calendar_month, 2) == 152


def test_multiple_streams_on_one_day_do_not_collide():
    day = datetime(2026, 5, 20, 9, 0)
    taken: set[tuple[int, int]] = set()
    assigned = []
    for _ in range(4):
        pair = assign_numbers(day, SeasonScheme.year, taken=taken)
        taken.add(pair)
        assigned.append(pair)

    assert len(set(assigned)) == 4
    episodes = [e for _s, e in assigned]
    assert episodes == sorted(episodes)


def test_format_episode_tag():
    assert format_episode_tag(2026, 1840) == "S2026E1840"
    assert format_episode_tag(1, 5) == "S01E0005"
