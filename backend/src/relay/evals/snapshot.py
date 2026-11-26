"""Normalized snapshots of every table a trial could change, and the difference between two.

Rows are keyed by their natural key (order number, RMA number, refund number, ...) and keep only
business fields: no internal IDs, no timestamps, no conversation or preview links. Line tables are
folded into their parent row. So a refund appears as
`{"refund_number": ..., "order": "PV-900001", "reason": ..., "amount_cents": ..., "lines": [...]}`,
and two trials that did the same thing produce the same normalized rows.

Previews (`action_previews`), the audit log and the conversation tables are not compared: they
record how the agent worked, not what it changed. The graders read them separately.
"""

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Row = dict[str, Any]
Snapshot = dict[str, dict[str, Row]]  # table -> natural key -> row

QUERIES: dict[str, str] = {
    "customers": """
        SELECT c.email AS key, c.email, c.full_name, c.phone FROM customers c
    """,
    "addresses": """
        SELECT c.email || '|' || a.line1 || '|' || a.postal_code AS key, c.email AS customer,
               a.recipient_name, a.line1, a.line2, a.city, a.state, a.postal_code, a.country,
               a.is_default
        FROM addresses a JOIN customers c ON c.id = a.customer_id
    """,
    "orders": """
        SELECT o.number AS key, o.number AS "order", c.email AS customer, o.status,
               o.shipping_address, o.payment_brand, o.payment_last4, o.shipping_cents,
               o.total_cents, o.customer_note, o.promised_delivery_on::text
        FROM orders o JOIN customers c ON c.id = o.customer_id
    """,
    "shipments": """
        SELECT o.number AS key, o.number AS "order", s.carrier, s.tracking_number, s.status,
               s.eta::text
        FROM shipments s JOIN orders o ON o.id = s.order_id
    """,
    "returns": """
        SELECT r.rma_number AS key, r.rma_number, o.number AS "order", r.status, r.reason,
               r.created_by_type,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('line', ol.line_no,
                                                             'quantity', rl.quantity)
                                          ORDER BY ol.line_no)
                         FROM return_lines rl JOIN order_lines ol ON ol.id = rl.order_line_id
                         WHERE rl.return_id = r.id), '[]'::jsonb) AS lines
        FROM returns r JOIN orders o ON o.id = r.order_id
    """,
    "refunds": """
        SELECT f.refund_number AS key, f.refund_number, o.number AS "order", f.reason,
               f.channel, f.amount_cents, f.status,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('kind', fl.kind,
                                                             'line', ol.line_no,
                                                             'quantity', fl.quantity,
                                                             'amount_cents', fl.amount_cents)
                                          ORDER BY fl.kind, ol.line_no)
                         FROM refund_lines fl LEFT JOIN order_lines ol ON ol.id = fl.order_line_id
                         WHERE fl.refund_id = f.id), '[]'::jsonb) AS lines
        FROM refunds f JOIN orders o ON o.id = f.order_id
    """,
    "approvals": """
        SELECT a.approval_number AS key, a.approval_number, o.number AS "order", a.action,
               a.status, a.amount_cents, a.rules, a.note
        FROM approvals a JOIN orders o ON o.id = a.order_id
    """,
    "escalations": """
        SELECT e.escalation_number AS key, e.escalation_number, c.email AS customer, e.reason,
               e.priority, e.status
        FROM escalations e JOIN customers c ON c.id = e.customer_id
    """,
}

TABLES = tuple(QUERIES)


async def snapshot(session: AsyncSession) -> Snapshot:
    taken: Snapshot = {}
    for table, query in QUERIES.items():
        result = await session.execute(text(query))
        rows: dict[str, Row] = {}
        for mapping in result.mappings():
            row = dict(mapping)
            rows[str(row.pop("key"))] = row
        taken[table] = rows
    return taken


@dataclass(frozen=True, slots=True)
class Change:
    table: str
    kind: Literal["insert", "update", "delete"]
    key: str
    row: Row  # the row after the change (before it, for a delete)
    before: Row | None = None

    def describe(self) -> str:
        return f"{self.kind} {self.table} {self.key}"


def diff(before: Snapshot, after: Snapshot) -> list[Change]:
    changes: list[Change] = []
    for table in TABLES:
        old, new = before.get(table, {}), after.get(table, {})
        for key in sorted(new.keys() - old.keys()):
            changes.append(Change(table, "insert", key, new[key]))
        for key in sorted(old.keys() & new.keys()):
            if old[key] != new[key]:
                changes.append(Change(table, "update", key, new[key], old[key]))
        for key in sorted(old.keys() - new.keys()):
            changes.append(Change(table, "delete", key, old[key]))
    return changes
