from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from app.models import Vod, VodMode, VodState
from app.services import library


def _vod(**overrides) -> Vod:
    defaults = {
        "id": 10,
        "channel_id": 1,
        "twitch_video_id": "2200112233",
        "title": "Ranked grind: road to masters",
        "description": "A long stream",
        "url": "https://www.twitch.tv/videos/2200112233",
        "thumbnail_url": None,
        "published_at": datetime(2026, 3, 4, 18, 30),
        "duration_s": 3 * 3600 + 21 * 60,
        "season": 2026,
        "episode": 630,
        "mode": VodMode.strm,
        "state": VodState.complete,
    }
    defaults.update(overrides)
    return Vod(**defaults)


def test_paths_follow_jellyfin_shows_layout(media_root: Path, sample_channel):
    vod = _vod()
    assert library.series_path(media_root, sample_channel).name == "Example Streamer"
    assert library.season_path(media_root, sample_channel, 2026).name == "Season 2026"
    assert (
        library.episode_basename(sample_channel, vod)
        == "Example Streamer - S2026E0630 - Ranked grind road to masters"
    )
    assert library.episode_path(media_root, sample_channel, vod, ".strm").suffix == ".strm"


def test_episode_titles_with_illegal_characters_are_sanitised(media_root: Path, sample_channel):
    vod = _vod(title='LIVE! <best/worst> plays: 100%?')
    name = library.episode_basename(sample_channel, vod)
    for illegal in '<>:"/\\|?*':
        assert illegal not in name


def test_tvshow_nfo_is_valid_and_locked(sample_channel):
    xml = library.build_tvshow_nfo(sample_channel)
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.tag == "tvshow"
    assert root.findtext("title") == "Example Streamer"
    assert root.findtext("studio") == "Twitch"
    # lockdata stops Jellyfin overwriting our metadata from TVDB/TMDB.
    assert root.findtext("lockdata") == "true"
    unique = root.find("uniqueid")
    assert unique is not None and unique.text == "123456"


def test_episode_nfo_includes_runtime_for_unprobed_strm_items(sample_channel):
    vod = _vod()
    xml = library.build_episode_nfo(sample_channel, vod)
    root = ET.fromstring(xml.split("\n", 1)[1])

    assert root.tag == "episodedetails"
    assert root.findtext("showtitle") == "Example Streamer"
    assert root.findtext("season") == "2026"
    assert root.findtext("episode") == "630"
    assert root.findtext("aired") == "2026-03-04"
    # 201 minutes - Jellyfin cannot probe remote .strm files during a scan, so
    # this is the only source of duration for strm episodes.
    assert root.findtext("runtime") == "201"


def test_strm_points_at_our_own_endpoint_with_token():
    vod = _vod()
    content = library.build_strm_content("http://jellyttv:8730/", vod, "tok123")
    assert content.strip() == "http://jellyttv:8730/vod/2200112233?key=tok123"
    assert content.endswith("\n")

    without = library.build_strm_content("http://jellyttv:8730", vod, None)
    assert without.strip() == "http://jellyttv:8730/vod/2200112233"


@pytest.mark.asyncio
async def test_publish_writes_full_tree_for_strm_mode(media_root: Path, sample_channel):
    vods = [_vod(), _vod(id=11, twitch_video_id="2200112234", episode=631, title="Second stream")]

    stats = await library.publish_channel(
        sample_channel,
        vods,
        media_root=media_root,
        self_base_url="http://jellyttv:8730",
        tuner_token="tok",
    )

    series = media_root / "Example Streamer"
    season = series / "Season 2026"
    assert stats.episodes_written == 2
    assert (series / "tvshow.nfo").exists()
    assert (season / "season.nfo").exists()

    strms = sorted(season.glob("*.strm"))
    nfos = sorted(p for p in season.glob("*.nfo") if p.name != "season.nfo")
    assert len(strms) == 2
    assert len(nfos) == 2
    assert "http://jellyttv:8730/vod/2200112233?key=tok" in strms[0].read_text().strip()


@pytest.mark.asyncio
async def test_publish_is_idempotent(media_root: Path, sample_channel):
    vods = [_vod()]
    for _ in range(3):
        stats = await library.publish_channel(
            sample_channel,
            vods,
            media_root=media_root,
            self_base_url="http://x:1",
            tuner_token=None,
        )
    season = media_root / "Example Streamer" / "Season 2026"
    assert len(list(season.glob("*.strm"))) == 1
    assert stats.files_removed == 0


@pytest.mark.asyncio
async def test_publish_prunes_orphans(media_root: Path, sample_channel):
    keep = _vod()
    drop = _vod(id=11, twitch_video_id="999", episode=631, title="Deleted stream")

    await library.publish_channel(
        sample_channel,
        [keep, drop],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    season = media_root / "Example Streamer" / "Season 2026"
    assert len(list(season.glob("*.strm"))) == 2

    stats = await library.publish_channel(
        sample_channel,
        [keep],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    remaining = list(season.glob("*.strm"))
    assert len(remaining) == 1
    assert "Deleted stream" not in remaining[0].name
    assert stats.files_removed >= 2  # .strm + .nfo


@pytest.mark.asyncio
async def test_archive_mode_skips_incomplete_downloads(media_root: Path, sample_channel):
    sample_channel.vod_mode = VodMode.archive
    pending = _vod(mode=VodMode.archive, state=VodState.downloading)

    stats = await library.publish_channel(
        sample_channel,
        [pending],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    assert stats.episodes_written == 0
    season = media_root / "Example Streamer" / "Season 2026"
    assert not list(season.glob("*.strm"))


@pytest.mark.asyncio
async def test_failed_archive_falls_back_to_a_strm_link(media_root: Path, sample_channel):
    sample_channel.vod_mode = VodMode.archive
    failed = _vod(mode=VodMode.archive, state=VodState.failed)

    stats = await library.publish_channel(
        sample_channel,
        [failed],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    assert stats.episodes_written == 1
    season = media_root / "Example Streamer" / "Season 2026"
    assert len(list(season.glob("*.strm"))) == 1


@pytest.mark.asyncio
async def test_vod_mode_off_writes_nothing(media_root: Path, sample_channel):
    sample_channel.vod_mode = VodMode.off
    stats = await library.publish_channel(
        sample_channel,
        [_vod()],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    assert stats.episodes_written == 0
    assert not (media_root / "Example Streamer").exists()


@pytest.mark.asyncio
async def test_rename_series_moves_files_instead_of_deleting(media_root: Path, sample_channel):
    """Renaming a channel folder must never destroy archived VODs."""
    await library.publish_channel(
        sample_channel,
        [_vod()],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    # Stand in for a large archived file.
    season = media_root / "Example Streamer" / "Season 2026"
    archived = season / "big-archive.mp4"
    archived.write_bytes(b"video-bytes")

    moved = library.rename_series(media_root, "Example Streamer", "New Name")

    assert moved == media_root / "New Name"
    assert not (media_root / "Example Streamer").exists()
    assert (media_root / "New Name" / "Season 2026" / "big-archive.mp4").read_bytes() == b"video-bytes"
    assert (media_root / "New Name" / "tvshow.nfo").exists()


def test_rename_series_is_a_noop_for_unchanged_or_missing_folders(media_root: Path):
    assert library.rename_series(media_root, "Same", "Same") is None
    assert library.rename_series(media_root, "Does Not Exist", "Other") is None


def test_rename_series_refuses_to_clobber_an_existing_folder(media_root: Path):
    (media_root / "A").mkdir()
    (media_root / "A" / "keep.txt").write_text("a")
    (media_root / "B").mkdir()

    assert library.rename_series(media_root, "A", "B") is None
    # Nothing was moved or destroyed.
    assert (media_root / "A" / "keep.txt").exists()


@pytest.mark.asyncio
async def test_remove_series_deletes_the_folder(media_root: Path, sample_channel):
    await library.publish_channel(
        sample_channel,
        [_vod()],
        media_root=media_root,
        self_base_url="http://x:1",
        tuner_token=None,
    )
    assert (media_root / "Example Streamer").exists()
    assert library.remove_series(media_root, sample_channel) is True
    assert not (media_root / "Example Streamer").exists()
    assert library.remove_series(media_root, sample_channel) is False
