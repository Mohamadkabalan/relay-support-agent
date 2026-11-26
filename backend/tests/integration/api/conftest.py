"""API tests run against a real server and a database of their own (one per test module).

The API commits for real (it runs turns in background tasks with their own sessions), so these
tests can't use the rolled-back connection of the other integration tests. Demo mode gives
every client a private copy of the personas, which keeps tests apart within the module.
"""

import asyncio
from collections.abc import Iterator

import pytest
from tests.support.factories import create_test_database, drop_test_database


@pytest.fixture(scope="module")
def api_database() -> Iterator[str]:
    url = asyncio.run(create_test_database(catalog=True))
    try:
        yield url
    finally:
        asyncio.run(drop_test_database(url))
