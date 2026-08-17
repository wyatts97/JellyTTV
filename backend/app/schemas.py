from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Channel, SeasonScheme, Settings, Vod, VodMode, VodState


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    twitch_client_id: str
    twitch_client_secret: str
    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    jellyfin_shows_library_id: str | None = None
    self_base_url: str | None = None
    public_base_url: str | None = None
    eventsub_enabled: bool = False


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    twitch_user_token: str | None = None

    eventsub_enabled: bool | None = None
    public_base_url: str | None = None
    self_base_url: str | None = None

    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    jellyfin_shows_library_id: str | None = None
    jellyfin_auto_refresh: bool | None = None

    tuner_include_offline: bool | None = None
    proxy_enabled: bool | None = None
    strip_ads: bool | None = None
    proxy_segments: bool | None = None
    default_quality: str | None = None
    guide_window_hours: int | None = Field(default=None, ge=6, le=336)

    default_vod_mode: VodMode | None = None
    default_retention_keep_count: int | None = Field(default=None, ge=0, le=10000)
    default_retention_max_gb: float | None = Field(default=None, ge=0)
    default_retention_max_age_days: int | None = Field(default=None, ge=0)


class SettingsOut(BaseModel):
    setup_complete: bool
    admin_username: str | None

    twitch_client_id: str | None
    twitch_client_secret_set: bool
    twitch_user_token_set: bool

    eventsub_enabled: bool
    eventsub_possible: bool
    public_base_url: str
    self_base_url: str
    eventsub_callback_url: str | None

    jellyfin_url: str | None
    jellyfin_api_key_set: bool
    jellyfin_shows_library_id: str | None
    jellyfin_auto_refresh: bool

    tuner_token: str | None
    tuner_include_offline: bool
    proxy_enabled: bool
    strip_ads: bool
    proxy_segments: bool
    default_quality: str
    guide_window_hours: int

    default_vod_mode: VodMode
    default_retention_keep_count: int | None
    default_retention_max_gb: float | None
    default_retention_max_age_days: int | None

    m3u_url: str
    xmltv_url: str


class ChannelCreate(BaseModel):
    channel: str = Field(description="Twitch login or twitch.tv URL")
    live_enabled: bool | None = None
    vod_mode: VodMode | None = None
    quality: str | None = None
    season_scheme: SeasonScheme | None = None
    retention_keep_count: int | None = None
    retention_max_gb: float | None = None
    retention_max_age_days: int | None = None


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    display_name: str | None = None
    series_dir: str | None = None
    enabled: bool | None = None
    live_enabled: bool | None = None
    vod_mode: VodMode | None = None
    quality: str | None = None
    season_scheme: SeasonScheme | None = None
    retention_keep_count: int | None = Field(default=None, ge=0, le=10000)
    retention_max_gb: float | None = Field(default=None, ge=0)
    retention_max_age_days: int | None = Field(default=None, ge=0)


class ChannelOut(BaseModel):
    id: int
    twitch_login: str
    twitch_user_id: str
    display_name: str
    avatar_url: str | None
    offline_image_url: str | None
    enabled: bool
    live_enabled: bool
    vod_mode: VodMode
    quality: str
    season_scheme: SeasonScheme
    series_dir: str
    retention_keep_count: int | None
    retention_max_gb: float | None
    retention_max_age_days: int | None
    is_live: bool
    live_title: str | None
    live_game: str | None
    live_viewers: int | None
    live_started_at: datetime | None
    live_thumbnail_url: str | None
    last_vod_sync_at: datetime | None
    last_error: str | None
    tvg_id: str
    stream_url: str
    library_path: str
    vod_counts: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        channel: Channel,
        *,
        stream_url: str,
        library_path: str,
        vod_counts: dict[str, int] | None = None,
    ) -> ChannelOut:
        return cls(
            id=channel.id or 0,
            twitch_login=channel.twitch_login,
            twitch_user_id=channel.twitch_user_id,
            display_name=channel.display_name,
            avatar_url=channel.avatar_url,
            offline_image_url=channel.offline_image_url,
            enabled=channel.enabled,
            live_enabled=channel.live_enabled,
            vod_mode=channel.vod_mode,
            quality=channel.quality,
            season_scheme=channel.season_scheme,
            series_dir=channel.series_dir,
            retention_keep_count=channel.retention_keep_count,
            retention_max_gb=channel.retention_max_gb,
            retention_max_age_days=channel.retention_max_age_days,
            is_live=channel.is_live,
            live_title=channel.live_title,
            live_game=channel.live_game,
            live_viewers=channel.live_viewers,
            live_started_at=channel.live_started_at,
            live_thumbnail_url=channel.live_thumbnail_url,
            last_vod_sync_at=channel.last_vod_sync_at,
            last_error=channel.last_error,
            tvg_id=channel.tvg_id,
            stream_url=stream_url,
            library_path=library_path,
            vod_counts=vod_counts or {},
        )


class VodOut(BaseModel):
    id: int
    channel_id: int
    channel_login: str | None = None
    twitch_video_id: str
    title: str
    url: str
    thumbnail_url: str | None
    published_at: datetime
    duration_s: int | None
    season: int
    episode: int
    mode: VodMode
    state: VodState
    file_path: str | None
    bytes: int | None
    progress: float
    attempts: int
    error: str | None

    @classmethod
    def build(cls, vod: Vod, *, channel_login: str | None = None) -> VodOut:
        return cls(
            id=vod.id or 0,
            channel_id=vod.channel_id,
            channel_login=channel_login,
            twitch_video_id=vod.twitch_video_id,
            title=vod.title,
            url=vod.url,
            thumbnail_url=f"/api/vods/{vod.id or 0}/thumbnail" if vod.thumbnail_url else None,
            published_at=vod.published_at,
            duration_s=vod.duration_s,
            season=vod.season,
            episode=vod.episode,
            mode=vod.mode,
            state=vod.state,
            file_path=vod.file_path,
            bytes=vod.bytes,
            progress=vod.progress,
            attempts=vod.attempts,
            error=vod.error,
        )


class ConnectionTest(BaseModel):
    ok: bool
    message: str
    details: dict = Field(default_factory=dict)


class JellyfinLibraryOut(BaseModel):
    id: str
    name: str
    collection_type: str | None
    locations: list[str]


def settings_out(row: Settings, *, resolved) -> SettingsOut:  # noqa: ANN001
    base = resolved.self_base_url
    token = f"?key={row.tuner_token}" if row.tuner_token else ""
    return SettingsOut(
        setup_complete=row.setup_complete,
        admin_username=row.admin_username,
        twitch_client_id=row.twitch_client_id,
        twitch_client_secret_set=bool(row.twitch_client_secret_enc),
        twitch_user_token_set=bool(row.twitch_user_token_enc),
        eventsub_enabled=row.eventsub_enabled,
        eventsub_possible=resolved.eventsub_possible,
        public_base_url=resolved.public_base_url,
        self_base_url=base,
        eventsub_callback_url=resolved.eventsub_callback_url(),
        jellyfin_url=row.jellyfin_url,
        jellyfin_api_key_set=bool(row.jellyfin_api_key_enc),
        jellyfin_shows_library_id=row.jellyfin_shows_library_id,
        jellyfin_auto_refresh=row.jellyfin_auto_refresh,
        tuner_token=row.tuner_token,
        tuner_include_offline=row.tuner_include_offline,
        proxy_enabled=row.proxy_enabled,
        strip_ads=row.strip_ads,
        proxy_segments=row.proxy_segments,
        default_quality=row.default_quality,
        guide_window_hours=row.guide_window_hours,
        default_vod_mode=row.default_vod_mode,
        default_retention_keep_count=row.default_retention_keep_count,
        default_retention_max_gb=row.default_retention_max_gb,
        default_retention_max_age_days=row.default_retention_max_age_days,
        m3u_url=f"{base}/tuner/playlist.m3u{token}",
        xmltv_url=f"{base}/tuner/guide.xml{token}",
    )
