"""The seed is deterministic, verifies clean and is fast; help-center search finds articles."""

import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.factories import NOW, make_engine, temporary_database

from relay.domain.policy_config import default_policy
from relay.kb.embeddings import HashingEmbedder
from relay.kb.ingest import ingest
from relay.kb.search import Mode, search
from relay.seed.run import seed_database
from relay.seed.verify import sample_orders, verify


async def test_seed_is_deterministic_fast_idempotent_and_verifies() -> None:
    policy = default_policy()
    async with temporary_database() as url:
        engine = make_engine(url)
        try:
            started = time.perf_counter()
            first = await seed_database(
                engine, seed=42, now=NOW, policy=policy, embedder=HashingEmbedder()
            )
            assert time.perf_counter() - started < 60
            assert first.seeded

            again = await seed_database(engine, seed=42, now=NOW, policy=policy, embedder=None)
            assert not again.seeded  # already there: nothing written
            assert again.data_hash == first.data_hash

            reseeded = await seed_database(
                engine, seed=42, now=NOW, policy=policy, embedder=None, reset=True
            )
            assert reseeded.seeded
            assert reseeded.data_hash == first.data_hash  # embeddings are outside the hash

            async with AsyncSession(engine) as session:
                report = await verify(session, policy)
                sample = await sample_orders(session, policy, 3)
            assert report.ok, report.failures
            assert len(sample) == 3
            counts = dict(report.stats["rows"])
            assert counts["help articles"] == 30
            assert counts["orders"] >= 2000
        finally:
            await engine.dispose()


@pytest.mark.parametrize("mode", list(Mode))
async def test_search_finds_the_obvious_article(session: AsyncSession, mode: Mode) -> None:
    embedder = HashingEmbedder()
    await ingest(session, default_policy(), embedder)
    hits = await search(
        session, "return window refurbished items", mode=mode, limit=5, embedder=embedder
    )
    slugs = [hit.slug for hit in hits]
    assert "return-policy" in slugs or "refurbished-products" in slugs
    assert len(slugs) == len(set(slugs)), "one hit per article"
    assert all(hit.excerpt for hit in hits)


async def test_search_edge_cases(session: AsyncSession) -> None:
    await ingest(session, default_policy(), None)
    assert await search(session, "   ", mode=Mode.FULL_TEXT) == []
    assert await search(session, "the a of", mode=Mode.FULL_TEXT) == []  # only stop words
    assert len(await search(session, "refund", mode=Mode.FULL_TEXT, limit=50)) <= 5
    with pytest.raises(ValueError, match="needs an embedder"):
        await search(session, "refund", mode=Mode.VECTOR)
