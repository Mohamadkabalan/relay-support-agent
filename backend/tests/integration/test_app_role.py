"""The application role (PLAN.md §11): data access, no DDL, an append-only audit log."""

import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from tests.support.factories import make_engine, server_url, temporary_database

from relay.db.roles import ensure_app_role, quote_literal


async def test_the_app_role_reads_and_writes_data_but_changes_nothing_else() -> None:
    role = f"relay_app_test_{secrets.token_hex(4)}"
    password = "test-" + secrets.token_hex(12) + "-o'brien"  # a quote, to exercise the quoting
    async with temporary_database(catalog=True) as url:
        owner = make_engine(url)
        try:
            await ensure_app_role(owner, role, password)
            await ensure_app_role(owner, role, password)  # idempotent: the ALTER path
            as_app = make_engine(
                make_url(url).set(username=role, password=password).render_as_string(False)
            )
            try:
                async with as_app.begin() as connection:
                    assert await connection.scalar(text("SELECT count(*) FROM products")) > 0
                    await connection.execute(
                        text(
                            "INSERT INTO audit_log (occurred_at, actor_type, actor_id, action, "
                            "entity, entity_id) VALUES (now(), 'system', 'test', 'test.write', "
                            "'test', '1')"
                        )
                    )
                    await connection.execute(text("UPDATE products SET name = name"))
                for forbidden in (
                    "UPDATE audit_log SET action = 'edited'",
                    "TRUNCATE audit_log",
                    "TRUNCATE messages",
                    "CREATE TABLE sneaky (id int)",
                    "DROP TABLE products",
                    "ALTER TABLE products ADD COLUMN sneaky int",
                    "DELETE FROM alembic_version",
                ):
                    with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
                        async with as_app.begin() as connection:
                            await connection.execute(text(forbidden))
            finally:
                await as_app.dispose()
        finally:
            async with owner.begin() as connection:
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))
            await owner.dispose()


def test_role_names_and_passwords_are_safe_in_sql() -> None:
    assert quote_literal("it's") == "'it''s'"
    with pytest.raises(ValueError, match="NUL"):
        quote_literal("a\x00b")


async def test_unsafe_role_names_are_refused() -> None:
    engine = make_engine(server_url())
    try:
        with pytest.raises(ValueError, match="plain role name"):
            await ensure_app_role(engine, "x; DROP DATABASE relay", "password-1234567890")
    finally:
        await engine.dispose()
