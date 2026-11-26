"""The template database and one throwaway clone per trial (PLAN.md §9.1, ADR-0008).

The template holds the migrations, the base seed (seed 42 on the frozen eval clock) and the help
center with its embeddings. It is built once and named after a fingerprint of everything that
goes into it, so a run reuses it until a migration, the seed, the help articles, the policy or
the embedder changes. It is built under a temporary name and renamed when complete, so a crash
never leaves a half-built template behind under the real name.

Each trial gets `CREATE DATABASE ... TEMPLATE` (a copy, not a re-seed) and drops it afterwards.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from relay.db import migrate
from relay.db.admin import create_database, drop_database
from relay.db.session import create_engine
from relay.domain.policy_config import PolicyConfig
from relay.kb.embeddings import Embedder
from relay.kb.ingest import ingest
from relay.seed import builders
from relay.seed.generate import GENERATOR_VERSION
from relay.seed.run import seed_database

# The frozen eval clock: a Tuesday afternoon, chosen to keep daylight-saving changes out of the
# seed window (§9.1).
EVAL_NOW = datetime(2026, 9, 15, 15, 0, tzinfo=ZoneInfo("America/New_York"))
EVAL_SEED = 42
TEMPLATE_PREFIX = "relay_eval_tpl_"
TRIAL_PREFIX = "relay_eval_t_"

# Source that decides what the template contains.
_INPUTS = ("relay.db.migrations.versions", "relay.seed", "relay.kb", "relay.kb.articles")


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    fingerprint: str
    built: bool  # False when an existing template was reused
    seconds: float


def fingerprint(policy: PolicyConfig, embedder: Embedder, *, base_seed: bool) -> str:
    digest = hashlib.sha256()
    for part in (
        migrate.head_revision() or "",
        str(EVAL_SEED),
        EVAL_NOW.isoformat(),
        GENERATOR_VERSION,
        policy.model_dump_json(),
        embedder.name,
        str(base_seed),
    ):
        digest.update(part.encode())
    for package in _INPUTS:
        root = Path(str(resources.files(package)))
        for path in sorted(root.glob("*")):
            if path.is_file() and path.suffix in {".py", ".md", ".yaml"}:
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


async def ensure_template(
    server_url: str,
    policy: PolicyConfig,
    embedder: Embedder,
    *,
    base_seed: bool = True,
    rebuild: bool = False,
) -> Template:
    started = time.perf_counter()
    stamp = fingerprint(policy, embedder, base_seed=base_seed)
    # One template per kind; a new fingerprint replaces only older templates of the same kind.
    kind = f"{'full' if base_seed else 'light'}_{'bge' if 'bge' in embedder.name else 'hash'}_"
    name = f"{TEMPLATE_PREFIX}{kind}{stamp}"
    existing = await databases(server_url, TEMPLATE_PREFIX + kind)
    if name in existing and not rebuild:
        return Template(name, stamp, built=False, seconds=time.perf_counter() - started)

    building = f"{name}_building_{secrets.token_hex(3)}"
    url = await create_database(server_url, building)
    engine = create_engine(url, pool_size=2)
    try:
        await migrate.upgrade(engine)
        if base_seed:
            await seed_database(
                engine, seed=EVAL_SEED, now=EVAL_NOW, policy=policy, embedder=embedder
            )
        else:  # the catalog and the help center only: enough for small test runs
            async with AsyncSession(engine) as session, session.begin():
                session.add_all(builders.products())
                await session.flush()
                await ingest(session, policy, embedder)
    except BaseException:
        await engine.dispose()
        await drop_database(server_url, building)
        raise
    await engine.dispose()
    for stale in existing:  # older fingerprints and leftovers of crashed builds
        await drop_database(server_url, stale)
    await _execute(server_url, f"ALTER DATABASE {building} RENAME TO {name}")
    return Template(name, stamp, built=True, seconds=time.perf_counter() - started)


async def clone(server_url: str, template: str) -> tuple[str, str]:
    """A fresh copy of the template for one trial: its name and URL."""
    name = f"{TRIAL_PREFIX}{secrets.token_hex(5)}"
    return name, await create_database(server_url, name, template=template)


async def drop(server_url: str, name: str) -> None:
    await drop_database(server_url, name)


async def databases(server_url: str, prefix: str) -> list[str]:
    engine = create_async_engine(server_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.scalars(
                text("SELECT datname FROM pg_database WHERE datname LIKE :pattern ORDER BY 1"),
                {"pattern": f"{prefix}%"},
            )
            return list(rows)
    finally:
        await engine.dispose()


async def _execute(server_url: str, statement: str) -> None:
    engine = create_async_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()
