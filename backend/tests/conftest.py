from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at throwaway directories for every test."""
    config_dir = tmp_path / "config"
    media_root = tmp_path / "media"
    config_dir.mkdir(parents=True, exist_ok=True)
    media_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("JELLYTTV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("JELLYTTV_MEDIA_ROOT", str(media_root))
    monkeypatch.setenv("JELLYTTV_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("JELLYTTV_REDIS_URL", "redis://127.0.0.1:6379/15")

    from app.config import get_config
    from app.crypto import reset_cipher_cache

    get_config.cache_clear()
    reset_cipher_cache()
    yield
    get_config.cache_clear()
    reset_cipher_cache()
    os.environ.pop("JELLYTTV_CONFIG_DIR", None)
    os.environ.pop("JELLYTTV_MEDIA_ROOT", None)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch: pytest.MonkeyPatch):
    """Tests run without Redis.

    `enqueue()` already treats a broker failure as "not queued", but letting it
    actually attempt a connection costs a timeout per call. Patching `get_pool`
    keeps that code path exercised while making it instant.
    """
    from app.worker import queue

    async def _unavailable():
        raise ConnectionError("redis is not available in tests")

    monkeypatch.setattr(queue, "get_pool", _unavailable)
    yield


@pytest.fixture
def media_root() -> Path:
    from app.config import get_config

    return get_config().media_root


@pytest.fixture
def sample_channel():
    from app.models import Channel, SeasonScheme, VodMode

    return Channel(
        id=1,
        twitch_login="examplestreamer",
        twitch_user_id="123456",
        display_name="Example Streamer",
        avatar_url=None,
        offline_image_url=None,
        description="A test channel",
        enabled=True,
        live_enabled=True,
        vod_mode=VodMode.strm,
        quality="best",
        season_scheme=SeasonScheme.year,
        series_dir="Example Streamer",
    )
