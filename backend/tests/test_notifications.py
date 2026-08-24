"""Go-live push notifications."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.crypto import encrypt
from app.models import Channel, Settings
from app.services import notifications
from app.services.jellyfin import JellyfinClient, JellyfinError
from app.services.settings_store import ResolvedSettings


def make_channel(**kwargs) -> Channel:
    defaults = {
        "twitch_login": "adapt",
        "twitch_user_id": "1",
        "display_name": "Adapt",
        "series_dir": "Adapt",
        "is_live": True,
        "live_title": "ALASKA MARATHON",
        "live_game": "Just Chatting",
        "live_viewers": 16840,
    }
    defaults.update(kwargs)
    return Channel(**defaults)


def make_settings(**kwargs) -> ResolvedSettings:
    row = Settings(
        jellyfin_url="http://jellyfin:8096",
        jellyfin_api_key_enc=encrypt("secret-key"),
        notify_on_live=True,
        **kwargs,
    )
    return ResolvedSettings(row=row)


# ------------------------------------------------------------------ templating
def test_templates_are_filled_from_live_state():
    channel = make_channel()
    assert notifications.render("{display_name} is live", channel) == "Adapt is live"
    assert notifications.render("{title} - {game}", channel) == "ALASKA MARATHON - Just Chatting"
    assert notifications.render("{viewers} watching", channel) == "16840 watching"


def test_unknown_placeholders_are_left_alone_rather_than_raising():
    """A typo in a user-supplied template must not silence the notification."""
    channel = make_channel()
    assert notifications.render("{nope} {display_name}", channel) == "{nope} Adapt"


def test_empty_body_falls_back_to_the_game():
    settings = make_settings(notify_body_template="{title}")
    channel = make_channel(live_title=None)
    _title, body = notifications.build_message(settings, channel)
    assert body == "Just Chatting"


def test_empty_title_template_falls_back_to_a_sensible_default():
    settings = make_settings(notify_title_template="")
    _title, _body = notifications.build_message(settings, make_channel())
    assert _title == "Adapt is live"


# -------------------------------------------------------------------- delivery
@respx.mock
async def test_notification_posts_to_the_streamyfin_endpoint():
    route = respx.post("http://jellyfin:8096/Streamyfin/notification").mock(
        return_value=httpx.Response(204)
    )
    settings = make_settings()
    sent = await notifications.notify_live(settings, make_channel())

    assert sent
    assert route.called
    request = route.calls.last.request
    payload = json.loads(request.content)
    # The endpoint takes an array of notifications.
    assert payload == [
        {
            "title": "Adapt is live",
            "body": "ALASKA MARATHON",
            "subtitle": "Just Chatting",
        }
    ]
    assert 'MediaBrowser Token="secret-key"' in request.headers["authorization"]


@respx.mock
async def test_a_missing_plugin_is_reported_as_not_configured_not_broken():
    respx.post("http://jellyfin:8096/Streamyfin/notification").mock(
        return_value=httpx.Response(404)
    )
    settings = make_settings()
    with pytest.raises(notifications.PluginMissing):
        await notifications.send(settings, "t", "b")

    # notify_live swallows it: an uninstalled plugin is a configuration state,
    # not a job failure to retry.
    assert await notifications.notify_live(settings, make_channel()) is False


@respx.mock
async def test_disabled_setting_sends_nothing():
    route = respx.post("http://jellyfin:8096/Streamyfin/notification").mock(
        return_value=httpx.Response(204)
    )
    settings = make_settings()
    settings.row.notify_on_live = False
    assert await notifications.notify_live(settings, make_channel()) is False
    assert not route.called


async def test_missing_jellyfin_config_is_an_error_not_a_crash():
    settings = make_settings()
    settings.row.jellyfin_url = None
    with pytest.raises(notifications.NotificationError):
        await notifications.send(settings, "t", "b")


@respx.mock
async def test_client_send_notification_raises_jellyfin_error_on_server_failure():
    respx.post("http://jellyfin:8096/Streamyfin/notification").mock(
        return_value=httpx.Response(500)
    )
    async with JellyfinClient("http://jellyfin:8096", "k") as client:
        with pytest.raises(JellyfinError):
            await client.send_notification(title="t", body="b")
