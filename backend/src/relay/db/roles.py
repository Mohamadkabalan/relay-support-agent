"""The application's database role (PLAN.md §11): data access only, no DDL.

The API and the MCP server connect as this role. Migrations, the seed and the nightly reset run
as the database owner, in the one-shot `migrate` job. `relay db upgrade` calls `ensure_app_role`
after migrating, so a new table is always granted before the services start.

The role can read and write the store and conversation tables, but:

- it can't create, alter or drop anything (no CREATE on the schema, owns nothing);
- on `audit_log` and `messages` it can only SELECT and INSERT. Their triggers already refuse
  UPDATE and DELETE; the revoked privileges also stop TRUNCATE, which a trigger can't.
"""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

APPEND_ONLY = ("audit_log", "messages")
ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def quote_literal(value: str) -> str:
    """A SQL string literal. CREATE ROLE takes no bind parameters for its password."""
    if "\x00" in value:
        raise ValueError("a password can't contain NUL")
    return "'" + value.replace("'", "''") + "'"


async def ensure_app_role(engine: AsyncEngine, role: str, password: str) -> None:
    """Create or update the login role and grant it exactly the data privileges it needs."""
    if not ROLE_NAME.fullmatch(role):
        raise ValueError(f"not a plain role name: {role!r}")
    secret = quote_literal(password)
    async with engine.begin() as connection:
        exists = await connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        verb = "ALTER" if exists else "CREATE"
        await connection.execute(
            text(
                f"{verb} ROLE {role} LOGIN PASSWORD {secret} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
        database = await connection.scalar(text("SELECT current_database()"))
        for statement in (
            f'GRANT CONNECT ON DATABASE "{database}" TO {role}',
            f"REVOKE CREATE ON SCHEMA public FROM {role}",
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
            f"REVOKE UPDATE, DELETE, TRUNCATE ON {', '.join(APPEND_ONLY)} FROM {role}",
            f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {role}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        ):
            await connection.execute(text(statement))
