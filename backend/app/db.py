from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

# SQLModel's AsyncSession (not SQLAlchemy's) is required: it provides `.exec()`,
# which returns model instances directly for `select(Model)` statements.
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import get_config
from app.logging_conf import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        cfg = get_config()
        cfg.ensure_dirs()
        _engine = create_async_engine(cfg.db_url, echo=False, future=True, pool_pre_ping=True)
        _apply_sqlite_pragmas(_engine)
        _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def init_db() -> None:
    """Create tables and run lightweight additive migrations.

    SQLite + a single-file self-hosted app means we can get away with
    `create_all` plus `ALTER TABLE ... ADD COLUMN` for new columns, which keeps
    upgrades a no-op for users. Destructive changes will require a real
    migration tool; see docs/upgrading.md.
    """
    engine = get_engine()
    # Import for side effects so SQLModel.metadata is populated.
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _add_missing_columns(conn)
    log.info("database ready", path=str(get_config().db_path))


async def _add_missing_columns(conn) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect

    def _collect(sync_conn):  # type: ignore[no-untyped-def]
        inspector = inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        statements: list[str] = []
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl_type = column.type.compile(sync_conn.dialect)
                statements.append(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
        return statements

    statements = await conn.run_sync(_collect)
    for statement in statements:
        log.info("applying additive migration", ddl=statement)
        await conn.execute(text(statement))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
