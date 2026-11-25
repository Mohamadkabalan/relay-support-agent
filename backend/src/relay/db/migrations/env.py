"""Alembic environment.

Two ways in:

- `relay db upgrade` (and the tests) pass an open connection in `config.attributes["connection"]`.
- `uv run alembic ...` from `backend/` (for `revision --autogenerate`) reads DATABASE_URL from the
  environment through the app settings.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import relay.db.models  # noqa: F401 (importing the models registers every table)
from relay.db.base import Base

target_metadata = Base.metadata


def run_with(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_from_settings() -> None:
    from relay.config import get_settings  # only the alembic CLI path needs settings

    engine = create_async_engine(str(get_settings().database_url))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_with)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported; run against a database.")

shared = context.config.attributes.get("connection")
if shared is not None:
    run_with(shared)
else:
    asyncio.run(run_from_settings())
