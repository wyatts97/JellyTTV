"""Database models.

All datetimes are stored as naive UTC. Use `app.util.utcnow()` to produce them.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlmodel import JSON, Column, Field, SQLModel, UniqueConstraint

from app.util import utcnow


class VodMode(str, enum.Enum):
    off = "off"
    strm = "strm"
    archive = "archive"


class VodState(str, enum.Enum):
    pending = "pending"
    queued = "queued"
    downloading = "downloading"
    complete = "complete"
    failed = "failed"
    skipped = "skipped"
    purged = "purged"


class JobState(str, enum.Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class SeasonScheme(str, enum.Enum):
    year = "year"
    calendar_month = "calendar_month"


class Settings(SQLModel, table=True):
    """Singleton configuration row (id is always 1)."""

    __tablename__ = "settings"

    id: int | None = Field(default=1, primary_key=True)

    setup_complete: bool = False

    admin_username: str | None = None
    admin_password_hash: str | None = None

    # Twitch
    twitch_client_id: str | None = None
    twitch_client_secret_enc: str | None = None
    twitch_user_token_enc: str | None = None  # optional, unlocks h265/1440p/ad-free

    # EventSub
    eventsub_secret_enc: str | None = None
    eventsub_enabled: bool = False
    public_base_url: str | None = None

    # Jellyfin
    jellyfin_url: str | None = None
    jellyfin_api_key_enc: str | None = None
    jellyfin_shows_library_id: str | None = None
    jellyfin_auto_refresh: bool = True

    # Go-live push notifications, delivered via the Streamyfin companion plugin
    # (Jellyfin's own web PWA cannot receive push at all).
    notify_on_live: bool = False
    notify_title_template: str = "{display_name} is live"
    notify_body_template: str = "{title}"

    # Base url of THIS service as reachable from the Jellyfin server.
    # Used inside .strm files and for the M3U/XMLTV urls shown in the UI.
    # e.g. http://jellyttv:8730 or http://192.168.1.10:8730
    self_base_url: str | None = None

    # Tuner / proxy behaviour
    tuner_token: str | None = None
    tuner_include_offline: bool = True
    proxy_enabled: bool = True
    strip_ads: bool = True
    proxy_segments: bool = False  # False = 302 redirect segments to Twitch CDN
    # Overrides the `playerType` sent with Twitch's access-token request. Twitch
    # stitches fewer (often zero) ads for player types other than its own `web`
    # default. Nullable because the additive migration in db.py adds columns
    # without a DEFAULT, so rows predating it read back NULL rather than the
    # default above; readers coerce through resolver.resolve_player_type.
    twitch_player_type: str | None = None
    # HTTP proxy used only for the Twitch token/manifest requests, so the token
    # is minted for a region that carries no ad inventory. Empty disables it.
    # Ignored whenever twitch_user_token is set - a credential must never be
    # routed through a third party. Defaults on; see resolver.DEFAULT_PROXY_URL.
    twitch_proxy_url: str | None = None
    # How ads are avoided. "ttv_ab" splices a clean backup stream over the
    # break, "ttv_lol_pro" mints the token through an ad-free-region proxy,
    # "strip_only" just removes ad segments. NULL means "never configured" and
    # resolves to the default - see services.adblock.
    ad_block_strategy: str | None = None
    # Allow a lower-quality backup when nothing clean exists at the stream's own
    # quality. TTV-AB's "Low Quality Fallback"; picture keeps moving at the cost
    # of resolution for the length of the break.
    ad_backup_low_quality: bool = True
    # Replay ad-progress telemetry for breaks that were blocked. Reports ads as
    # watched that were not; separate from blocking and independently toggleable.
    ad_spoofing: bool = True
    default_quality: str = "best"
    guide_window_hours: int = 48

    # Retention defaults applied to newly added channels
    default_vod_mode: VodMode = Field(default=VodMode.strm)
    default_retention_keep_count: int | None = 10
    default_retention_max_gb: float | None = None
    default_retention_max_age_days: int | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Channel(SQLModel, table=True):
    __tablename__ = "channel"
    __table_args__ = (UniqueConstraint("twitch_login", name="uq_channel_login"),)

    id: int | None = Field(default=None, primary_key=True)

    twitch_login: str = Field(index=True)
    twitch_user_id: str = Field(index=True)
    display_name: str

    avatar_url: str | None = None
    offline_image_url: str | None = None
    description: str | None = None

    enabled: bool = True
    live_enabled: bool = True
    vod_mode: VodMode = Field(default=VodMode.strm)
    quality: str = "best"

    season_scheme: SeasonScheme = Field(default=SeasonScheme.year)
    series_dir: str

    retention_keep_count: int | None = None
    retention_max_gb: float | None = None
    retention_max_age_days: int | None = None

    # Denormalised live state for fast dashboard/guide rendering
    is_live: bool = False
    live_title: str | None = None
    live_game: str | None = None
    live_viewers: int | None = None
    live_started_at: datetime | None = None
    live_thumbnail_url: str | None = None

    last_vod_sync_at: datetime | None = None
    last_error: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def tvg_id(self) -> str:
        return f"twitch.{self.twitch_login}"


class StreamSession(SQLModel, table=True):
    __tablename__ = "stream_session"
    __table_args__ = (UniqueConstraint("twitch_stream_id", name="uq_session_stream_id"),)

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)

    twitch_stream_id: str = Field(index=True)
    title: str | None = None
    game_name: str | None = None
    thumbnail_url: str | None = None

    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    is_live: bool = True
    viewer_peak: int | None = None


class Vod(SQLModel, table=True):
    __tablename__ = "vod"
    __table_args__ = (UniqueConstraint("twitch_video_id", name="uq_vod_video_id"),)

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)

    twitch_video_id: str = Field(index=True)
    title: str = ""
    description: str | None = None
    url: str = ""
    thumbnail_url: str | None = None

    published_at: datetime = Field(default_factory=utcnow, index=True)
    duration_s: int | None = None

    season: int = 0
    episode: int = 0

    mode: VodMode = Field(default=VodMode.strm)
    state: VodState = Field(default=VodState.pending, index=True)

    file_path: str | None = None
    bytes: int | None = None
    progress: float = 0.0
    attempts: int = 0
    error: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "job"

    id: int | None = Field(default=None, primary_key=True)
    type: str = Field(index=True)
    key: str | None = Field(default=None, index=True)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    state: JobState = Field(default=JobState.queued, index=True)
    progress: float = 0.0
    message: str | None = None
    log_tail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class EventSubSubscription(SQLModel, table=True):
    __tablename__ = "eventsub_subscription"

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int | None = Field(default=None, foreign_key="channel.id", index=True)
    twitch_sub_id: str = Field(index=True)
    type: str
    status: str = "unknown"
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None


class EventSubMessage(SQLModel, table=True):
    """Replay protection - stores seen Twitch message ids."""

    __tablename__ = "eventsub_message"

    message_id: str = Field(primary_key=True)
    received_at: datetime = Field(default_factory=utcnow, index=True)


class EventLog(SQLModel, table=True):
    __tablename__ = "event_log"

    id: int | None = Field(default=None, primary_key=True)
    level: str = "info"
    category: str = "general"
    message: str = ""
    channel_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
