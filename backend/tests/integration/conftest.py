"""Database fixtures for the integration tests.

One database is created per test session: migrated, with the catalog loaded. Each test runs in a
transaction that is rolled back afterwards, so tests never see each other's rows. Tests that need
real commits (concurrency, seeding, migrations) create a database of their own with
`temporary_database`.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from tests.support.factories import (
    NOW,
    Factory,
    create_test_database,
    drop_test_database,
    make_engine,
    rollback_sessionmaker,
)

from relay.clock import FrozenClock
from relay.domain.policy_config import PolicyConfig, default_policy
from relay.domain.scope import Deps


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """The shared, migrated test database with the catalog loaded."""
    # Two separate event loops: an async context manager can't span them, because closing the
    # first loop would finalize it (and drop the database) right away.
    url = asyncio.run(create_test_database(catalog=True))
    try:
        yield url
    finally:
        asyncio.run(drop_test_database(url))


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One connection inside a transaction that is rolled back after the test."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back after the test, even if the code under test commits."""
    db = rollback_sessionmaker(connection)()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def policy() -> PolicyConfig:
    return default_policy()


@pytest.fixture
def deps(session: AsyncSession, clock: FrozenClock, policy: PolicyConfig) -> Deps:
    return Deps(session=session, clock=clock, policy=policy)


@pytest.fixture
def factory(session: AsyncSession, clock: FrozenClock, policy: PolicyConfig) -> Factory:
    return Factory(session, clock, policy)
