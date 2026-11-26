"""A small eval template for these tests: migrations, the catalog and the help center (hashing
embedder), without the 500-customer base seed. Built once per test session, dropped at the end."""

import asyncio
from collections.abc import Iterator

import pytest
from tests.support.factories import server_url

from relay.config import Settings
from relay.domain.policy_config import default_policy
from relay.evals import databases
from relay.evals.databases import Template, ensure_template
from relay.kb.embeddings import HashingEmbedder


@pytest.fixture(scope="session")
def light_template() -> Iterator[Template]:
    url = server_url()
    template = asyncio.run(
        ensure_template(url, default_policy(), HashingEmbedder(), base_seed=False)
    )
    try:
        yield template
    finally:
        asyncio.run(databases.drop(url, template.name))


@pytest.fixture
def eval_settings(live_settings: Settings) -> Settings:
    return live_settings
