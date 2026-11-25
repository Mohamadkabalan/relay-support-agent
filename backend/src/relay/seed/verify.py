"""`relay seed --verify`: invariants the seeded data must satisfy, plus a summary of what's there.

Each invariant is a query that returns the offending rows, so an empty result means it holds.
The persona checks run the real policy engine on each persona's scenario.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from relay.db.models import Customer, SeedRun
from relay.domain.errors import OrderNotFoundError
from relay.domain.facts import facts_for, load_owned_order
from relay.domain.policy import evaluate
from relay.domain.policy_config import PolicyConfig
from relay.seed.personas import PERSONAS

INVARIANTS: dict[str, str] = {
    "every order has at least one line": """
        SELECT o.number FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM order_lines l WHERE l.order_id = o.id)""",
    "order totals equal lines plus shipping": """
        SELECT o.number FROM orders o JOIN order_lines l ON l.order_id = o.id
        GROUP BY o.id
        HAVING o.total_cents <> sum(l.unit_price_cents * l.quantity) + o.shipping_cents""",
    "refund amounts equal their lines": """
        SELECT r.refund_number FROM refunds r JOIN refund_lines l ON l.refund_id = r.id
        GROUP BY r.id HAVING r.amount_cents <> sum(l.amount_cents)""",
    "refunds never exceed what was paid": """
        SELECT o.number FROM orders o JOIN refunds r ON r.order_id = o.id
        GROUP BY o.id HAVING sum(r.amount_cents) > o.total_cents""",
    "returned plus refunded units never exceed units bought": """
        WITH returned AS (
            SELECT rl.order_line_id, sum(rl.quantity) AS qty
            FROM return_lines rl JOIN returns r ON r.id = rl.return_id
            WHERE r.status <> 'cancelled' GROUP BY 1),
        refunded AS (
            SELECT fl.order_line_id, sum(fl.quantity) AS qty
            FROM refund_lines fl JOIN refunds f ON f.id = fl.refund_id
            WHERE f.return_id IS NULL AND fl.kind = 'item' GROUP BY 1)
        SELECT o.number || ' line ' || l.line_no FROM order_lines l
        JOIN orders o ON o.id = l.order_id
        LEFT JOIN returned ON returned.order_line_id = l.id
        LEFT JOIN refunded ON refunded.order_line_id = l.id
        WHERE coalesce(returned.qty, 0) + coalesce(refunded.qty, 0) > l.quantity""",
    "shipping is refunded at most once per order": """
        SELECT o.number FROM orders o JOIN refunds r ON r.order_id = o.id
        JOIN refund_lines l ON l.refund_id = r.id AND l.kind = 'shipping'
        GROUP BY o.id HAVING count(*) > 1""",
    "nothing happens in the future": """
        SELECT 'order ' || number FROM orders WHERE placed_at > :now
        UNION ALL SELECT 'customer ' || email FROM customers WHERE created_at > :now
        UNION ALL SELECT 'event ' || id FROM shipment_events WHERE occurred_at > :now
        UNION ALL SELECT 'return ' || rma_number FROM returns WHERE created_at > :now
        UNION ALL SELECT 'refund ' || refund_number FROM refunds WHERE issued_at > :now""",
    "timelines run forwards": """
        SELECT o.number FROM orders o JOIN shipments s ON s.order_id = o.id
        WHERE s.shipped_at < o.placed_at OR s.delivered_at < s.shipped_at
        UNION ALL
        SELECT o.number FROM orders o JOIN shipments s ON s.order_id = o.id
        JOIN shipment_events e ON e.shipment_id = s.id WHERE e.occurred_at < s.shipped_at
        UNION ALL
        SELECT number FROM orders WHERE cancelled_at < placed_at
        UNION ALL
        SELECT r.rma_number FROM returns r JOIN shipments s ON s.order_id = r.order_id
        WHERE r.created_at < s.delivered_at OR s.delivered_at IS NULL
        UNION ALL
        SELECT f.refund_number FROM refunds f JOIN orders o ON o.id = f.order_id
        WHERE f.issued_at < o.placed_at""",
    "order status matches the shipment": """
        SELECT o.number FROM orders o LEFT JOIN shipments s ON s.order_id = o.id
        WHERE (o.status = 'delivered' AND (s.status IS DISTINCT FROM 'delivered'
                                           OR s.delivered_at IS NULL))
           OR (o.status = 'shipped' AND (s.id IS NULL OR s.status = 'delivered'))
           OR (o.status IN ('pending', 'processing', 'cancelled') AND s.id IS NOT NULL)
           OR ((s.status = 'delivered') <> (s.delivered_at IS NOT NULL))""",
    "returns are inside the window and returnable": """
        SELECT r.rma_number FROM returns r
        JOIN return_lines rl ON rl.return_id = r.id
        JOIN order_lines l ON l.id = rl.order_line_id
        JOIN products p ON p.sku = l.sku
        JOIN shipments s ON s.order_id = r.order_id
        WHERE (r.created_at AT TIME ZONE :tz)::date
              > (s.delivered_at AT TIME ZONE :tz)::date
                + CASE WHEN p.condition = 'refurbished' THEN CAST(:refurb_days AS int)
                       ELSE CAST(:window_days AS int) END
           OR p.final_sale OR p.kind <> 'physical'""",
    "damage refunds are inside the window": """
        SELECT f.refund_number FROM refunds f
        JOIN refund_lines fl ON fl.refund_id = f.id AND fl.kind = 'item'
        JOIN order_lines l ON l.id = fl.order_line_id
        JOIN products p ON p.sku = l.sku
        JOIN shipments s ON s.order_id = f.order_id
        WHERE f.reason = 'damaged_or_defective'
          AND (f.issued_at AT TIME ZONE :tz)::date
              > (s.delivered_at AT TIME ZONE :tz)::date
                + CASE WHEN p.condition = 'refurbished' THEN CAST(:refurb_days AS int)
                       ELSE CAST(:window_days AS int) END""",
}

STATS: dict[str, str] = {
    "orders by status": "SELECT status, count(*) FROM orders GROUP BY 1 ORDER BY 2 DESC",
    "shipments by carrier status": (
        "SELECT status, count(*) FROM shipments GROUP BY 1 ORDER BY 2 DESC"
    ),
    "returns by status": "SELECT status, count(*) FROM returns GROUP BY 1 ORDER BY 2 DESC",
    "refunds by reason and channel": (
        "SELECT reason || ' / ' || channel, count(*) FROM refunds GROUP BY 1 ORDER BY 2 DESC"
    ),
    "deliveries 2+ days late": """
        SELECT CASE WHEN o.shipping_cents > 0 THEN 'with a shipping fee' ELSE 'free shipping' END,
               count(*)
        FROM orders o JOIN shipments s ON s.order_id = o.id
        WHERE (s.delivered_at AT TIME ZONE :tz)::date - o.promised_delivery_on
              >= CAST(:grace AS int)
        GROUP BY 1""",
    "in transit by days since the last scan": """
        SELECT CASE WHEN idle >= CAST(:lost_days AS int) THEN 'lost rule applies'
                    ELSE 'not lost yet' END, count(*)
        FROM (SELECT CAST(:today AS date) - max(e.occurred_at AT TIME ZONE :tz)::date AS idle
              FROM shipments s JOIN shipment_events e ON e.shipment_id = s.id
              WHERE s.status = 'in_transit' GROUP BY s.id) t
        GROUP BY 1""",
    "customers with agent refunds in the velocity window": """
        SELECT refunds || ' agent refunds', count(*) FROM (
            SELECT o.customer_id, count(*) AS refunds FROM refunds r
            JOIN orders o ON o.id = r.order_id
            WHERE r.channel = 'agent'
              AND r.issued_at >= CAST(:now AS timestamptz)
                                 - make_interval(days => CAST(:velocity_days AS int))
            GROUP BY 1) t GROUP BY 1 ORDER BY 1""",
    "orders with a customer note": (
        "SELECT 'notes', count(*) FROM orders WHERE customer_note IS NOT NULL"
    ),
    "rows": """
        SELECT 'customers', count(*) FROM customers UNION ALL
        SELECT 'products', count(*) FROM products UNION ALL
        SELECT 'orders', count(*) FROM orders UNION ALL
        SELECT 'order lines', count(*) FROM order_lines UNION ALL
        SELECT 'shipment events', count(*) FROM shipment_events UNION ALL
        SELECT 'help articles', count(*) FROM help_articles UNION ALL
        SELECT 'kb chunks', count(*) FROM kb_chunks""",
}


@dataclass(slots=True)
class VerifyReport:
    seeded_now: datetime | None
    failures: dict[str, list[str]] = field(default_factory=dict)
    stats: dict[str, list[tuple[str, int]]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures


async def verify(session: AsyncSession, policy: PolicyConfig) -> VerifyReport:
    seed_run = await session.scalar(select(SeedRun).order_by(SeedRun.created_at.desc()).limit(1))
    if seed_run is None:
        return VerifyReport(seeded_now=None, failures={"seeded": ["no seed run recorded"]})
    now = seed_run.seeded_now
    params = {
        "now": now,
        "today": now.astimezone(policy.tz).date(),
        "tz": policy.store_timezone,
        "window_days": policy.returns.window_days,
        "refurb_days": policy.returns.window_days_refurbished,
        "grace": policy.refunds.late_delivery_grace_days,
        "lost_days": policy.refunds.lost_package_days_without_scan,
        "velocity_days": policy.refunds.velocity_window_days,
    }
    report = VerifyReport(seeded_now=now)
    for name, query in INVARIANTS.items():
        rows = (await session.execute(text(query), params)).scalars().all()
        if rows:
            report.failures[name] = [str(row) for row in rows[:5]]

    for persona in PERSONAS:
        customer = await session.scalar(select(Customer).where(Customer.email == persona.email))
        if customer is None:
            report.failures[f"persona {persona.key}"] = ["missing"]
            continue
        for check in persona.checks:
            try:
                order = await load_owned_order(session, customer.id, check.order_number)
            except OrderNotFoundError:
                report.failures[f"persona {persona.key}"] = [f"{check.order_number} missing"]
                continue
            facts = await facts_for(session, order, policy, now)
            decision = evaluate(check.request, facts, policy, now)
            if decision.outcome is not check.expected:
                report.failures[f"persona {persona.key}"] = [
                    f"{check.order_number}: expected {check.expected.value}, "
                    f"got {decision.outcome.value} {decision.rules}"
                ]

    for name, query in STATS.items():
        rows = (await session.execute(text(query), params)).all()
        report.stats[name] = [(str(label), int(count)) for label, count in rows]
    return report


SAMPLE_QUERY = """
    SELECT o.number, c.full_name, o.status, s.status AS shipment,
           (o.placed_at AT TIME ZONE :tz)::date AS placed_on, o.total_cents,
           string_agg(l.quantity || ' x ' || l.name, ', ' ORDER BY l.line_no) AS items
    FROM orders o
    JOIN customers c ON c.id = o.customer_id
    JOIN order_lines l ON l.order_id = o.id
    LEFT JOIN shipments s ON s.order_id = o.id
    GROUP BY o.id, c.full_name, s.status
    ORDER BY md5(o.number)
    LIMIT :limit
"""


async def sample_orders(
    session: AsyncSession, policy: PolicyConfig, limit: int
) -> list[tuple[str, str, str, str | None, str, int, str]]:
    rows = await session.execute(text(SAMPLE_QUERY), {"tz": policy.store_timezone, "limit": limit})
    return [
        (number, name, status, shipment, str(placed_on), total, items)
        for number, name, status, shipment, placed_on, total, items in rows
    ]
