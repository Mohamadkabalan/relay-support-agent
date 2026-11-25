"""Creating and dropping whole databases: for tests now, and for eval trial databases in Phase 4."""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from relay.db.session import with_database

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _check(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe database name {name!r}")
    return name


async def create_database(server_url: str, name: str, *, template: str | None = None) -> str:
    """Create `name` on the server behind `server_url` and return its URL.

    With `template`, Postgres copies that database file by file, which is much faster than
    migrating and seeding again (the eval harness relies on this).
    """
    clause = f" TEMPLATE {_check(template)}" if template else ""
    engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {_check(name)}{clause}"))
    finally:
        await engine.dispose()
    return with_database(server_url, name)


async def drop_database(server_url: str, name: str) -> None:
    engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {_check(name)} WITH (FORCE)"))
    finally:
        await engine.dispose()
