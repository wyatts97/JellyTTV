"""The additive-migration path, which has taken the settings endpoint down once.

`ALTER TABLE ... ADD COLUMN` with no DEFAULT fills existing rows with NULL. When
the response model declares that field non-optional, every read of the endpoint
then fails validation with a 500 - and because the UI rendered a spinner for
"no data", the whole settings page hung with nothing on screen explaining why.
These tests pin down both halves of the fix: new columns arrive populated, and
rows already damaged by an earlier upgrade get repaired on the next start.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import get_engine, init_db, session_scope
from app.services.settings_store import get_settings_row

# The three columns that were NULL in production and 500'd /api/settings.
DAMAGED = ("notify_on_live", "notify_title_template", "notify_body_template")


async def _seed() -> None:
    await init_db()
    async with session_scope() as session:
        await get_settings_row(session)


async def test_new_column_is_added_with_its_default_not_null():
    """A column added to an existing table must not leave NULLs behind."""
    await _seed()
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.execute(text('ALTER TABLE "settings" DROP COLUMN "notify_on_live"'))
        names = {row[1] for row in await conn.execute(text('PRAGMA table_info("settings")'))}
        assert "notify_on_live" not in names

    # Re-running startup is what a container restart does after an upgrade.
    await init_db()

    async with engine.begin() as conn:
        value = (await conn.execute(text('SELECT "notify_on_live" FROM "settings"'))).scalar_one()
        assert value is not None


async def test_nulls_left_by_an_earlier_migration_are_backfilled():
    """The exact production failure: NULL notify_* columns 500ing /api/settings.

    Reproduced the way it actually happened - the columns are re-added the way
    the old migration added them, with no DEFAULT. SQLite makes an ALTER-added
    column nullable regardless of what the model says, which is why the rows
    could hold NULL in a column declared NOT NULL in the metadata.
    """
    await _seed()
    engine = get_engine()

    async with engine.begin() as conn:
        for column in DAMAGED:
            await conn.execute(text(f'ALTER TABLE "settings" DROP COLUMN "{column}"'))
        for column, ddl_type in zip(DAMAGED, ("BOOLEAN", "VARCHAR", "VARCHAR")):
            await conn.execute(
                text(f'ALTER TABLE "settings" ADD COLUMN "{column}" {ddl_type}')
            )
        for column in DAMAGED:
            value = (
                await conn.execute(text(f'SELECT "{column}" FROM "settings"'))
            ).scalar_one()
            assert value is None, f"{column} should start out NULL for this test"

    await init_db()

    async with engine.begin() as conn:
        for column in DAMAGED:
            value = (
                await conn.execute(text(f'SELECT "{column}" FROM "settings"'))
            ).scalar_one()
            assert value is not None, f"{column} was left NULL"


async def test_nullable_columns_are_left_alone():
    """Backfilling a genuinely optional column would invent data."""
    await _seed()
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.execute(text('UPDATE "settings" SET "jellyfin_url" = NULL'))

    await init_db()

    async with engine.begin() as conn:
        value = (await conn.execute(text('SELECT "jellyfin_url" FROM "settings"'))).scalar_one()
        assert value is None
