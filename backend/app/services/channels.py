from __future__ import annotations

from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.logging_conf import get_logger
from app.models import Channel, EventLog, StreamSession, VodMode
from app.services.settings_store import ResolvedSettings
from app.services.twitch import TwitchClient
from app.util import normalise_channel_input, parse_twitch_time, twitch_thumbnail, utcnow

log = get_logger(__name__)


class ChannelError(RuntimeError):
    pass


async def list_channels(session: AsyncSession, *, enabled_only: bool = False) -> list[Channel]:
    statement = select(Channel).order_by(Channel.display_name)
    if enabled_only:
        statement = statement.where(Channel.enabled == True)  # noqa: E712
    return list((await session.exec(statement)).all())


async def get_channel(session: AsyncSession, channel_id: int) -> Channel | None:
    return await session.get(Channel, channel_id)


async def get_channel_by_login(session: AsyncSession, login: str) -> Channel | None:
    statement = select(Channel).where(Channel.twitch_login == login.lower())
    return (await session.exec(statement)).first()


async def get_channel_by_user_id(session: AsyncSession, user_id: str) -> Channel | None:
    statement = select(Channel).where(Channel.twitch_user_id == str(user_id))
    return (await session.exec(statement)).first()


async def add_channel(
    session: AsyncSession,
    settings: ResolvedSettings,
    raw_input: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> Channel:
    login = normalise_channel_input(raw_input)
    if not login:
        raise ChannelError("Enter a Twitch channel name or URL")

    existing = await get_channel_by_login(session, login)
    if existing:
        raise ChannelError(f"{existing.display_name} is already being tracked")

    if not settings.twitch_configured:
        raise ChannelError("Configure your Twitch client id and secret first")

    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        user = await twitch.get_user(login)
        if not user:
            raise ChannelError(f'Twitch has no channel called "{login}"')
        live = await twitch.get_streams([user["id"]])

    row = channel_from_twitch_user(settings, user)
    for key, value in (overrides or {}).items():
        if value is not None and hasattr(row, key):
            setattr(row, key, value)

    session.add(row)
    await session.commit()
    await session.refresh(row)

    if live:
        await apply_live_payload(session, row, live[0])

    session.add(
        EventLog(
            category="channel",
            message=f"Added channel {row.display_name}",
            channel_id=row.id,
        )
    )
    await session.commit()
    log.info("channel added", login=row.twitch_login, user_id=row.twitch_user_id)
    return row


def channel_from_twitch_user(settings: ResolvedSettings, user: dict[str, Any]) -> Channel:
    row = settings.row
    return Channel(
        twitch_login=user["login"].lower(),
        twitch_user_id=str(user["id"]),
        display_name=user.get("display_name") or user["login"],
        avatar_url=user.get("profile_image_url"),
        offline_image_url=user.get("offline_image_url") or None,
        description=user.get("description") or None,
        series_dir=user.get("display_name") or user["login"],
        vod_mode=row.default_vod_mode or VodMode.strm,
        quality=row.default_quality or "best",
        retention_keep_count=row.default_retention_keep_count,
        retention_max_gb=row.default_retention_max_gb,
        retention_max_age_days=row.default_retention_max_age_days,
    )


async def refresh_profiles(
    session: AsyncSession, settings: ResolvedSettings, channels: list[Channel]
) -> int:
    """Refresh avatars / offline images / display names from Helix."""
    if not channels or not settings.twitch_configured:
        return 0

    ids = [c.twitch_user_id for c in channels]
    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        users = await twitch.get_users(ids=ids)

    by_id = {str(u["id"]): u for u in users}
    updated = 0
    for channel in channels:
        user = by_id.get(channel.twitch_user_id)
        if not user:
            continue
        channel.display_name = user.get("display_name") or channel.display_name
        channel.avatar_url = user.get("profile_image_url") or channel.avatar_url
        channel.offline_image_url = user.get("offline_image_url") or channel.offline_image_url
        channel.description = user.get("description") or channel.description
        channel.updated_at = utcnow()
        session.add(channel)
        updated += 1
    await session.commit()
    return updated


async def poll_live_state(
    session: AsyncSession, settings: ResolvedSettings, channels: list[Channel]
) -> dict[str, bool]:
    """Poll Helix for live state. Returns {login: is_live} for changed channels."""
    if not channels or not settings.twitch_configured:
        return {}

    async with TwitchClient(settings.twitch_client_id, settings.twitch_client_secret) as twitch:
        streams = await twitch.get_streams([c.twitch_user_id for c in channels])

    live_by_id = {str(s["user_id"]): s for s in streams}
    changed: dict[str, bool] = {}

    for channel in channels:
        payload = live_by_id.get(channel.twitch_user_id)
        was_live = channel.is_live
        if payload:
            await apply_live_payload(session, channel, payload)
            if not was_live:
                changed[channel.twitch_login] = True
        else:
            if was_live:
                await mark_offline(session, channel)
                changed[channel.twitch_login] = False
    return changed


async def apply_live_payload(
    session: AsyncSession, channel: Channel, payload: dict[str, Any]
) -> StreamSession:
    stream_id = str(payload.get("id") or payload.get("stream_id") or "")
    started_at = parse_twitch_time(payload.get("started_at")) or utcnow()

    channel.is_live = True
    channel.live_title = payload.get("title") or channel.live_title
    channel.live_game = payload.get("game_name") or channel.live_game
    channel.live_viewers = payload.get("viewer_count")
    channel.live_started_at = started_at
    channel.live_thumbnail_url = twitch_thumbnail(payload.get("thumbnail_url"))
    channel.updated_at = utcnow()
    session.add(channel)

    row: StreamSession | None = None
    if stream_id:
        row = (
            await session.exec(
                select(StreamSession).where(StreamSession.twitch_stream_id == stream_id)
            )
        ).first()

    if row is None:
        row = StreamSession(
            channel_id=channel.id,  # type: ignore[arg-type]
            twitch_stream_id=stream_id or f"synthetic-{channel.id}-{int(started_at.timestamp())}",
            started_at=started_at,
        )

    row.title = channel.live_title
    row.game_name = channel.live_game
    row.thumbnail_url = channel.live_thumbnail_url
    row.is_live = True
    row.ended_at = None
    if payload.get("viewer_count") is not None:
        row.viewer_peak = max(row.viewer_peak or 0, int(payload["viewer_count"]))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def mark_offline(session: AsyncSession, channel: Channel) -> None:
    channel.is_live = False
    channel.live_viewers = None
    channel.updated_at = utcnow()
    session.add(channel)

    sessions = (
        await session.exec(
            select(StreamSession)
            .where(StreamSession.channel_id == channel.id)
            .where(StreamSession.is_live == True)  # noqa: E712
        )
    ).all()
    for row in sessions:
        row.is_live = False
        row.ended_at = utcnow()
        session.add(row)
    await session.commit()


async def apply_channel_update(
    session: AsyncSession, channel: Channel, *, title: str | None, category: str | None
) -> None:
    """Handle a `channel.update` EventSub notification (title/category change)."""
    if title:
        channel.live_title = title
    if category is not None:
        channel.live_game = category or None
    channel.updated_at = utcnow()
    session.add(channel)

    if channel.is_live:
        row = (
            await session.exec(
                select(StreamSession)
                .where(StreamSession.channel_id == channel.id)
                .where(StreamSession.is_live == True)  # noqa: E712
            )
        ).first()
        if row:
            row.title = channel.live_title
            row.game_name = channel.live_game
            session.add(row)
    await session.commit()
