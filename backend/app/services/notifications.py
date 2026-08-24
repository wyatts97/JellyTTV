"""Go-live push notifications.

Delivery goes through the Streamyfin companion plugin's notification endpoint,
because Jellyfin itself has no way to push to a client that is not currently
open: its web PWA ships a service worker that only does offline caching, with no
Web Push subscription.
"""

from __future__ import annotations

from app.logging_conf import get_logger
from app.models import Channel
from app.services.jellyfin import JellyfinClient, JellyfinError
from app.services.settings_store import ResolvedSettings

log = get_logger(__name__)


class NotificationError(RuntimeError):
    pass


class PluginMissing(NotificationError):
    """The Streamyfin plugin is not installed on the Jellyfin server."""


def render(template: str, channel: Channel) -> str:
    """Fill a notification template from a channel's live state.

    Unknown placeholders are left alone rather than raising: a typo in a user
    supplied template must not silence the notification entirely.
    """
    values = {
        "display_name": channel.display_name or channel.twitch_login,
        "login": channel.twitch_login,
        "title": channel.live_title or "",
        "game": channel.live_game or "",
        "viewers": str(channel.live_viewers or 0),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out.strip()


def build_message(settings: ResolvedSettings, channel: Channel) -> tuple[str, str]:
    row = settings.row
    title = render(row.notify_title_template, channel) or (
        f"{channel.display_name} is live"
    )
    body = render(row.notify_body_template, channel)
    if not body:
        # An empty body reads as a broken notification; fall back to the game.
        body = channel.live_game or "Live now on Twitch"
    return title, body


async def send(settings: ResolvedSettings, title: str, body: str, *, subtitle: str | None = None) -> None:
    """Send one notification. Raises `PluginMissing` if Streamyfin is absent."""
    if not settings.jellyfin_configured:
        raise NotificationError("Jellyfin url and API key are required")

    async with JellyfinClient(settings.row.jellyfin_url, settings.jellyfin_api_key) as client:
        try:
            await client.send_notification(title=title, body=body, subtitle=subtitle)
        except JellyfinError as exc:
            if exc.status == 404:
                raise PluginMissing(
                    "the Streamyfin plugin is not installed on this Jellyfin server"
                ) from exc
            raise NotificationError(str(exc)) from exc


async def notify_live(settings: ResolvedSettings, channel: Channel) -> bool:
    """Announce that a channel went live. Returns whether anything was sent."""
    if not settings.row.notify_on_live:
        return False
    title, body = build_message(settings, channel)
    try:
        await send(settings, title, body, subtitle=channel.live_game or None)
    except PluginMissing as exc:
        log.warning("go-live notification skipped", login=channel.twitch_login, error=str(exc))
        return False
    return True
