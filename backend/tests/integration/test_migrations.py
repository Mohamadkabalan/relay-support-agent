"""The migration runs both ways on an empty database and matches the ORM models."""

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support.factories import make_engine, temporary_database

from relay.db import migrate
from relay.db.base import Base


async def test_upgrade_downgrade_upgrade_on_an_empty_database() -> None:
    async with temporary_database(migrated=False) as url:
        engine = make_engine(url)
        try:
            await migrate.upgrade(engine)
            assert await migrate.current_revision(engine) == migrate.head_revision()
            assert "audit_log" in await _tables(engine)

            await migrate.downgrade(engine, "base")
            assert await migrate.current_revision(engine) is None
            assert await _tables(engine) == {"alembic_version"}

            await migrate.upgrade(engine)
            assert len(await _tables(engine)) == len(Base.metadata.tables) + 1
        finally:
            await engine.dispose()


async def test_models_and_migrations_agree() -> None:
    async with temporary_database() as url:
        engine = make_engine(url)
        try:
            async with engine.connect() as connection:
                diff = await connection.run_sync(_diff)
        finally:
            await engine.dispose()
    assert diff == [], "the ORM models changed without a migration (uv run alembic revision)"


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}
