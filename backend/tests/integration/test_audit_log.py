"""The audit log is append-only, enforced by the database itself."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

INSERT = text("""
    INSERT INTO audit_log (occurred_at, actor_type, actor_id, action, entity, entity_id)
    VALUES (now(), 'system', 'test', 'test.write', 'test', :entity_id)
""")


async def test_rows_can_be_added(session: AsyncSession) -> None:
    await session.execute(INSERT, {"entity_id": str(uuid.uuid4())})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET reason = 'edited'",
        "DELETE FROM audit_log",
        "DELETE FROM audit_log WHERE false",  # rejected even when nothing would change
    ],
)
async def test_rows_cannot_be_changed_or_removed(session: AsyncSession, statement: str) -> None:
    await session.execute(INSERT, {"entity_id": str(uuid.uuid4())})
    with pytest.raises(DBAPIError, match="audit_log is append-only"):
        await session.execute(text(statement))
