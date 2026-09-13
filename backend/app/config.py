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
    #
    # This short TTL exists only to stop unrelated callers respawning streamlink;
    # live playback does NOT use it. A stream session pins the url it resolved
    # and keeps it until upstream actually fails, because re-resolving hands back
    # a different video-weaver host whose segment numbering does not line up with
    # what the player has already buffered.
    resolver_cache_seconds: int = 20
    resolver_session_ttl_seconds: int = 3600

    # Idle time before an inactive stream session is discarded.
    stream_session_idle_seconds: int = 90

    # Live delivery as MPEG-TS from `streamlink --stdout` (services.live_stream).
    # One streamlink process per open stream, so this caps the processes too.
    max_live_streams: int = 8
    # How long to wait for the first TS bytes before answering 503. streamlink
    # has to fetch an access token, the master playlist and the first segment
    # before anything reaches stdout.
    live_startup_timeout_seconds: float = 30.0
    # Passed straight through to streamlink's own HLS client.
    live_segment_attempts: int = 3
    live_segment_timeout_seconds: float = 10.0
    live_stream_timeout_seconds: float = 60.0
    live_hls_live_edge: int = 3
    # Twitch declares EXT-X-TARGETDURATION:6 over 2-second segments, and
    # streamlink reloads at the target duration by default - so it would pick up
    # three segments at a time and emit them in 6-second bursts. "segment"
    # reloads once per segment instead, keeping the byte flow smooth.
    live_playlist_reload_time: str = "segment"
    # streamlink never refreshes its token or re-resolves mid-stream: when its
    # playlist url stops working it just exits. Rather than end the response
    # and rely on Jellyfin to reopen the tuner, the stream restarts streamlink in
    # place. These bound that: spawn attempts per restart, the backoff between
    # them, and how many restarts in a 5-minute window before giving up.
    live_restart_attempts: int = 5
    live_restart_backoff_seconds: float = 1.0
    live_max_restarts_per_window: int = 6

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
