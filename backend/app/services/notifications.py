"""Go-live push notifications.

Two independent delivery channels, sharing one set of message templates:

* **Web Push** from JellyTTV's own PWA, to every browser or installed app that
  subscribed (see services.webpush). Tapping it opens the built-in player.
* The **Streamyfin** companion plugin's notification endpoint on Jellyfin, for
  people watching in a Jellyfin client. Jellyfin itself cannot push to a client
  that is not open: its web PWA's service worker only does offline caching.

Either can fail or be switched off without affecting the other.
"""

from __future__ import annotations

from sqlmodel.ext.asyncio.session import AsyncSession

from app.logging_conf import get_logger
from app.models import Channel
from app.services import webpush
from app.services.jellyfin import JellyfinClient, JellyfinError
from app.services.settings_store import ResolvedSettings
from app.util import utcnow

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


def web_push_payload(settings: ResolvedSettings, channel: Channel) -> dict:
    """What the service worker turns into a notification.

    Image urls are root-relative: the service worker resolves them against its
    own origin, which is whatever origin the device subscribed from.
    """
    title, body = build_message(settings, channel)
    login = channel.twitch_login
    return {
        "title": title,
        "body": body,
        "icon": f"/api/channels/{channel.id}/avatar",
        # Cache-busted so a phone never shows the previous broadcast's preview.
        "image": f"/api/channels/{channel.id}/thumbnail?v={int(utcnow().timestamp())}",
        # One notification per channel: a flapping stream replaces, not stacks.
        "tag": f"live-{login}",
        "url": f"/watch/{login}",
    }


async def notify_live(
    settings: ResolvedSettings, channel: Channel, session: AsyncSession | None = None
) -> bool:
    """Announce that a channel went live. Returns whether anything was sent.

    Web Push needs a database session (the subscriptions live there), so it is
    skipped when none is given.
    """
    if not channel.notify_enabled:
        return False
    sent = False

    if session is not None and settings.row.webpush_enabled:
        try:
            report = await webpush.send_all(session, settings, web_push_payload(settings, channel))
        except Exception as exc:  # noqa: BLE001 - must not block the Jellyfin channel
            log.warning("web push failed", login=channel.twitch_login, error=str(exc))
        else:
            sent = sent or report.sent > 0
            if report.total:
                log.info(
                    "go-live web push delivered",
                    login=channel.twitch_login,
                    sent=report.sent,
                    failed=report.failed,
                    removed=report.removed,
                )

    if settings.row.notify_on_live:
        title, body = build_message(settings, channel)
        try:
            await send(settings, title, body, subtitle=channel.live_game or None)
        except PluginMissing as exc:
            log.warning("go-live notification skipped", login=channel.twitch_login, error=str(exc))
        except NotificationError as exc:
            if not sent:
                raise
            log.warning("streamyfin notification failed", login=channel.twitch_login, error=str(exc))
        else:
            sent = True
    return sent
