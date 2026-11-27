"""The nightly demo reset (PLAN.md §12): fresh data for today, everyone signed out, spend kept."""

import os
import secrets
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from tests.support.factories import make_engine, temporary_database
from typer.testing import CliRunner

from relay.cli import app
from relay.config import get_settings
from relay.db.models import Customer, SeedRun
from relay.domain.policy_config import default_policy
from relay.seed.reset import reset_demo


async def test_the_reset_reseeds_for_now_and_forgets_sessions_but_not_spend() -> None:
    prefix = f"relay-test-{secrets.token_hex(4)}:"
    redis = Redis.from_url(os.environ["REDIS_URL"])
    now = datetime(2026, 12, 1, 4, 0, tzinfo=UTC)
    try:
        for key in ("session:a", "session:b", "events:c", "spend:2026-12-01", "rate:send:x:1"):
            await redis.set(prefix + key, "1")
        async with temporary_database() as url:
            engine = make_engine(url)
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO operators (id, email, display_name) VALUES "
                            "(gen_random_uuid(), 'old@relay.example', 'Old')"
                        )
                    )
                outcome = await reset_demo(
                    engine,
                    redis,
                    prefix=prefix,
                    seed=42,
                    now=now,
                    policy=default_policy(),
                    embedder=None,
                )
                async with engine.connect() as connection:
                    customers = await connection.scalar(select(func.count()).select_from(Customer))
                    run = (await connection.execute(select(SeedRun.seeded_now))).scalar_one()
                    operators = await connection.scalar(
                        text("SELECT count(*) FROM operators WHERE email = 'old@relay.example'")
                    )
            finally:
                await engine.dispose()
        remaining = sorted(
            [key.decode().removeprefix(prefix) async for key in redis.scan_iter(match=prefix + "*")]
        )
    finally:
        async for key in redis.scan_iter(match=prefix + "*"):
            await redis.delete(key)
        await redis.aclose()

    assert outcome.seed.seeded
    assert outcome.keys_deleted == 3
    assert remaining == ["rate:send:x:1", "spend:2026-12-01"]
    assert customers is not None
    assert customers > 500  # 500 customers plus the demo personas
    assert run == now
    assert operators == 0


@pytest.mark.parametrize(
    ("env", "args", "code", "message"),
    [
        ({"RELAY_DEMO_MODE": "false"}, ["demo", "reset", "--yes"], 1, "RELAY_DEMO_MODE is off"),
        ({"RELAY_DEMO_MODE": "true"}, ["demo", "reset"], 2, "pass --yes"),
    ],
)
def test_the_reset_refuses_outside_the_demo_and_without_yes(
    env: dict[str, str], args: list[str], code: int, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, args)
    finally:
        get_settings.cache_clear()
    assert result.exit_code == code
    assert message in result.output
