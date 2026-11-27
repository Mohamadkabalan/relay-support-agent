"""`relay seed`: migrate-independent seeding of a database, idempotent by default."""

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.db.models import SeedRun
from relay.db.session import make_sessionmaker
from relay.domain.policy_config import PolicyConfig
from relay.kb.embeddings import Embedder
from relay.kb.ingest import ingest
from relay.seed.generate import GENERATOR_VERSION, generate
from relay.seed.load import data_hash, insert, is_empty, wipe


@dataclass(frozen=True, slots=True)
class SeedOutcome:
    seeded: bool  # False when the data was already there and --reset wasn't given
    seed: int
    seeded_now: datetime
    data_hash: str
    seconds: float


async def seed_database(
    engine: AsyncEngine,
    *,
    seed: int,
    now: datetime,
    policy: PolicyConfig,
    embedder: Embedder | None,
    reset: bool = False,
    customers: int = 500,
    orders: int = 2000,
) -> SeedOutcome:
    started = time.perf_counter()
    async with make_sessionmaker(engine)() as session, session.begin():
        if reset:
            # Always, even with no customers: operators, conversations or help articles left
            # without them would otherwise survive a reset.
            await wipe(session)
        elif not await is_empty(session):
            existing = await session.scalar(
                select(SeedRun).order_by(SeedRun.created_at.desc()).limit(1)
            )
            return SeedOutcome(
                seeded=False,
                seed=existing.seed if existing else -1,
                seeded_now=existing.seeded_now if existing else now,
                data_hash=existing.data_hash if existing else "",
                seconds=time.perf_counter() - started,
            )

        data = generate(seed=seed, now=now, policy=policy, customers=customers, orders=orders)
        await insert(session, data)
        await ingest(session, policy, embedder)
        digest = await data_hash(session)
        session.add(
            SeedRun(
                seed=seed,
                seeded_now=now,
                generator_version=GENERATOR_VERSION,
                data_hash=digest,
            )
        )
    return SeedOutcome(
        seeded=True,
        seed=seed,
        seeded_now=now,
        data_hash=digest,
        seconds=time.perf_counter() - started,
    )
