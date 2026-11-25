"""Engines and sessions.

Domain services take an `AsyncSession` and never commit: the caller owns the transaction (a tool
call, an API request, a seed run). That keeps a write and its audit row in one transaction, and
lets tests run every service inside a transaction that is rolled back afterwards.
"""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from relay.config import Settings


def create_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        pool_pre_ping=True,
        # Every connection works in UTC; business dates are converted to the store time zone
        # explicitly (relay.clock.local_date), never by the database session.
        connect_args={"server_settings": {"timezone": "UTC"}},
    )


def engine_from_settings(settings: Settings) -> AsyncEngine:
    return create_engine(str(settings.database_url))


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def database_name(url: str) -> str:
    name = make_url(url).database
    if not name:
        raise ValueError("The database URL has no database name")
    return name


def with_database(url: str, name: str) -> str:
    """The same server and credentials, another database."""
    return make_url(url).set(database=name).render_as_string(hide_password=False)
