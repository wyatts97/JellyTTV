from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_config
from app.crypto import decrypt, encrypt, random_eventsub_secret, random_token
from app.models import Settings
from app.util import utcnow

SECRET_FIELDS = {
    "twitch_client_secret": "twitch_client_secret_enc",
    "twitch_user_token": "twitch_user_token_enc",
    "jellyfin_api_key": "jellyfin_api_key_enc",
    "eventsub_secret": "eventsub_secret_enc",
}


@dataclass(slots=True)
class ResolvedSettings:
    """Settings with secrets decrypted, safe to pass around inside the app."""

    row: Settings

    @property
    def twitch_client_id(self) -> str | None:
        return self.row.twitch_client_id

    @property
    def twitch_client_secret(self) -> str | None:
        return decrypt(self.row.twitch_client_secret_enc)

    @property
    def twitch_user_token(self) -> str | None:
        return decrypt(self.row.twitch_user_token_enc)

    @property
    def jellyfin_api_key(self) -> str | None:
        return decrypt(self.row.jellyfin_api_key_enc)

    @property
    def eventsub_secret(self) -> str | None:
        return decrypt(self.row.eventsub_secret_enc)

    @property
    def public_base_url(self) -> str:
        return (self.row.public_base_url or get_config().normalised_public_base_url()).rstrip("/")

    @property
    def self_base_url(self) -> str:
        """Base url of this service as reachable from Jellyfin."""
        explicit = (self.row.self_base_url or "").rstrip("/")
        if explicit:
            return explicit
        if self.public_base_url:
            return self.public_base_url
        return f"http://localhost:{get_config().port}"

    @property
    def twitch_configured(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.row.jellyfin_url and self.jellyfin_api_key)

    @property
    def eventsub_possible(self) -> bool:
        return self.public_base_url.startswith("https://")

    def eventsub_callback_url(self) -> str | None:
        if not self.eventsub_possible:
            return None
        return f"{self.public_base_url}/eventsub/callback"


async def get_settings_row(session: AsyncSession) -> Settings:
    row = (await session.exec(select(Settings).where(Settings.id == 1))).first()
    if row is None:
        cfg = get_config()
        row = Settings(
            id=1,
            tuner_token=random_token(),
            eventsub_secret_enc=encrypt(random_eventsub_secret()),
            public_base_url=cfg.normalised_public_base_url() or None,
            guide_window_hours=cfg.guide_window_hours,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    if not row.tuner_token:
        row.tuner_token = random_token()
        session.add(row)
        await session.commit()
    return row


async def get_settings(session: AsyncSession) -> ResolvedSettings:
    return ResolvedSettings(await get_settings_row(session))


async def update_settings(session: AsyncSession, values: dict) -> Settings:
    row = await get_settings_row(session)
    for key, value in values.items():
        if value is None:
            continue
        if key in SECRET_FIELDS:
            # Empty string means "clear it", any other value replaces it.
            setattr(row, SECRET_FIELDS[key], encrypt(value) if value != "" else None)
            continue
        if key in {"id", "created_at", "admin_password_hash"}:
            continue
        if hasattr(row, key):
            setattr(row, key, value)
    if row.public_base_url:
        row.public_base_url = row.public_base_url.rstrip("/")
    row.updated_at = utcnow()
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
