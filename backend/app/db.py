from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum

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


def _scalar_default(column):  # type: ignore[no-untyped-def]
    """The column's Python-side default, if it is a plain value.

    Callable defaults (timestamps) are skipped: they need a context the DDL
    cannot supply, and the ORM populates them on insert anyway.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    return value.value if isinstance(value, Enum) else value


def _sql_literal(value) -> str | None:  # type: ignore[no-untyped-def]
    """Render a scalar default as a SQLite DDL literal."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


async def _add_missing_columns(conn) -> None:  # type: ignore[no-untyped-def]
    """Add columns the model gained, and make sure they are not left NULL.

    `ADD COLUMN` without a DEFAULT fills every existing row with NULL, which a
    non-optional field on the response model then refuses to serialise - one
    added column takes out the whole endpoint with a 500, and the UI has no way
    to explain it. So new columns carry their model default into the DDL, and
    any column the model declares NOT NULL is backfilled afterwards. The
    backfill is what repairs databases already damaged by an earlier upgrade;
    it is a no-op once they are clean.
    """
    from sqlalchemy import inspect

    def _collect(sync_conn):  # type: ignore[no-untyped-def]
        inspector = inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        statements: list[str] = []
        backfills: list[tuple[str, str, object]] = []
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                default = _scalar_default(column)
                if column.name not in have:
                    ddl_type = column.type.compile(sync_conn.dialect)
                    ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
                    literal = _sql_literal(default) if default is not None else None
                    if literal is not None:
                        ddl += f" DEFAULT {literal}"
                    statements.append(ddl)
                if not column.nullable and default is not None:
                    backfills.append((table.name, column.name, default))
        return statements, backfills

    statements, backfills = await conn.run_sync(_collect)
    for statement in statements:
        log.info("applying additive migration", ddl=statement)
        await conn.execute(text(statement))

    for table_name, column_name, default in backfills:
        result = await conn.execute(
            text(
                f'UPDATE "{table_name}" SET "{column_name}" = :value '
                f'WHERE "{column_name}" IS NULL'
            ),
            {"value": default},
        )
        if result.rowcount:
            log.info(
                "backfilled null column left by an earlier migration",
                table=table_name,
                column=column_name,
                rows=result.rowcount,
            )


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
