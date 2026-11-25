"""Run Alembic migrations from Python: the `relay db` commands, the seed and the tests."""

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

SCRIPT_LOCATION = "relay.db:migrations"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_run, "upgrade", revision)


async def downgrade(engine: AsyncEngine, revision: str) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_run, "downgrade", revision)


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_revision()
        )


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _run(connection: Connection, direction: str, revision: str) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    if direction == "upgrade":
        command.upgrade(config, revision)
    else:
        command.downgrade(config, revision)
