"""The nightly demo reset (PLAN.md §12): `relay demo reset`, run by a systemd timer at 04:00 UTC.

1. Wipe every seeded and runtime row (store, conversations, approvals, audit log, visitor
   copies) and seed again relative to the current time, so the demo personas' stories (a
   package late by three days, a return on day 29) stay true every day.
2. Delete the Redis keys that point into the old data: sessions, which name customers and
   operators that no longer exist, and the conversations' event streams.

The daily spend counters and the rate-limit windows stay: a reset must not reopen the spend cap.
It runs as the database owner (the migrate job), because the wipe truncates the audit log.
"""

from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.domain.policy_config import PolicyConfig
from relay.kb.embeddings import Embedder
from relay.seed.run import SeedOutcome, seed_database

FORGOTTEN = ("session:*", "events:*")  # under the app's Redis prefix


@dataclass(frozen=True, slots=True)
class ResetOutcome:
    seed: SeedOutcome
    keys_deleted: int


async def forget_runtime_keys(redis: Redis, prefix: str) -> int:
    deleted = 0
    for pattern in FORGOTTEN:
        batch: list[bytes | str] = []
        async for key in redis.scan_iter(match=prefix + pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                deleted += await redis.delete(*batch)
                batch = []
        if batch:
            deleted += await redis.delete(*batch)
    return deleted


async def reset_demo(
    engine: AsyncEngine,
    redis: Redis,
    *,
    prefix: str,
    seed: int,
    now: datetime,
    policy: PolicyConfig,
    embedder: Embedder | None,
) -> ResetOutcome:
    outcome = await seed_database(
        engine, seed=seed, now=now, policy=policy, embedder=embedder, reset=True
    )
    return ResetOutcome(outcome, await forget_runtime_keys(redis, prefix))
