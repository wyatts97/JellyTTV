"""Process-level configuration.

Only things that cannot sensibly be changed at runtime live here (paths, redis
url, session secret). Everything else - Twitch credentials, Jellyfin details,
per-channel behaviour - lives in the database and is editable from the web UI.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JELLYTTV_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    config_dir: Path = Path("./data/config")
    media_root: Path = Path("./data/media")

    redis_url: str = "redis://127.0.0.1:6379/0"

    port: int = 8730
    session_secret: str = "insecure-development-secret-change-me"
    session_cookie: str = "jellyttv_session"
    session_max_age: int = 60 * 60 * 24 * 14

    # Public https base url, e.g. https://jellyttv.example.com
    # Required for Twitch EventSub webhooks; empty means polling-only mode.
    public_base_url: str = ""

    log_level: str = "INFO"
    log_format: str = "console"

    # How long a resolved upstream HLS playlist url is reused before we ask
    # streamlink again. Twitch tokens outlive this comfortably.
    resolver_cache_seconds: int = 20

    # Guide window (hours) rendered into the XMLTV output.
    guide_window_hours: int = 48

    # Max concurrent yt-dlp archive downloads.
    max_concurrent_downloads: int = 2

    # Minimum free disk space (GiB) required before starting a download.
    min_free_disk_gib: float = 5.0

    @property
    def db_path(self) -> Path:
        return self.config_dir / "jellyttv.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret.key"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"

    def normalised_public_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_config() -> AppConfig:
    cfg = AppConfig()
    cfg.ensure_dirs()
    return cfg
