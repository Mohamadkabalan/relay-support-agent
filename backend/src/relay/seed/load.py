"""Writing generated rows to the database, wiping them, and fingerprinting the result."""

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.seed.generate import SeedData

# Every table the seed owns, in an order that is safe to TRUNCATE together.
DATA_TABLES = (
    "conversation_events",
    "conversation_inbox",
    "tool_calls",
    "model_calls",
    "messages",
    "turns",
    "conversations",
    "audit_log",
    "escalations",
    "refund_lines",
    "refunds",
    "approvals",
    "return_lines",
    "returns",
    "action_previews",
    "shipment_events",
    "shipments",
    "order_lines",
    "orders",
    "addresses",
    "customers",
    "products",
    "operators",
    "kb_chunks",
    "help_articles",
    "seed_runs",
)

SEQUENCE_STARTS = {
    "rma_number_seq": 100001,
    "refund_number_seq": 10001,
    "approval_number_seq": 10001,
    "escalation_number_seq": 10001,
}

# What the data hash covers. Embeddings are left out on purpose: ONNX Runtime can produce slightly
# different floats on different CPUs, and the hash should identify the data, not the machine.
HASHED_QUERIES = {
    "products": "SELECT p.* FROM products p",
    "customers": "SELECT c.* FROM customers c",
    "addresses": "SELECT a.* FROM addresses a",
    "orders": "SELECT o.* FROM orders o",
    "order_lines": "SELECT l.* FROM order_lines l",
    "shipments": "SELECT s.* FROM shipments s",
    "shipment_events": "SELECT e.* FROM shipment_events e",
    "returns": "SELECT r.* FROM returns r",
    "return_lines": "SELECT l.* FROM return_lines l",
    "refunds": "SELECT r.* FROM refunds r",
    "refund_lines": "SELECT l.* FROM refund_lines l",
    "help_articles": "SELECT a.* FROM help_articles a",
    "kb_chunks": "SELECT c.id, c.article_id, c.ordinal, c.heading, c.text FROM kb_chunks c",
}


async def insert(session: AsyncSession, data: SeedData) -> None:
    session.add_all(data.products)
    await session.flush()
    session.add_all(data.customers)
    session.add_all(data.orders)
    await session.flush()
    session.add_all(data.returns)
    await session.flush()
    session.add_all(data.refunds)
    await session.flush()
    # Runtime numbers continue after the seeded ones.
    await session.execute(text("SELECT setval('rma_number_seq', :n)"), {"n": data.last_rma})
    await session.execute(text("SELECT setval('refund_number_seq', :n)"), {"n": data.last_refund})


async def wipe(session: AsyncSession) -> None:
    """Delete every seeded and runtime row. Needs the table owner (TRUNCATE bypasses the audit
    log's UPDATE/DELETE trigger by design)."""
    await session.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    for name, start in SEQUENCE_STARTS.items():
        await session.execute(text(f"ALTER SEQUENCE {name} RESTART WITH {start}"))


async def is_empty(session: AsyncSession) -> bool:
    count = await session.scalar(text("SELECT count(*) FROM customers"))
    return not count


async def data_hash(session: AsyncSession) -> str:
    """A SHA-256 over every seeded row, independent of physical row order."""
    await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
    digest = hashlib.sha256()
    for table, query in HASHED_QUERIES.items():
        table_md5 = await session.scalar(
            text(
                f"SELECT coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') "  # noqa: S608
                f"FROM ({query}) t"
            )
        )
        digest.update(f"{table}:{table_md5}\n".encode())
    return digest.hexdigest()
