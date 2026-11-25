"""The generator, without a database: deterministic, consistent, and shaped like PLAN.md §8."""

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from relay.domain.enums import OrderStatus
from relay.domain.policy_config import default_policy
from relay.seed.catalog import BY_SKU, CATALOG
from relay.seed.generate import SeedData, generate
from relay.seed.personas import PERSONA_EMAILS, PERSONAS

NOW = datetime(2026, 9, 15, 15, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture(scope="module")
def data() -> SeedData:
    return generate(seed=42, now=NOW, policy=default_policy())


def _fingerprint(data: SeedData) -> list[tuple[object, ...]]:
    return [
        (
            o.id,
            o.number,
            o.status,
            o.placed_at,
            o.total_cents,
            [(line.sku, line.quantity) for line in o.lines],
        )
        for o in data.orders
    ]


def test_same_seed_same_rows(data: SeedData) -> None:
    again = generate(seed=42, now=NOW, policy=default_policy())
    assert _fingerprint(again) == _fingerprint(data)
    assert [c.email for c in again.customers] == [c.email for c in data.customers]


def test_different_seed_different_rows(data: SeedData) -> None:
    other = generate(seed=43, now=NOW, policy=default_policy(), customers=50, orders=100)
    assert {o.number for o in other.orders} != {o.number for o in data.orders}


def test_sizes(data: SeedData) -> None:
    assert len(data.customers) == 500 + len(PERSONAS)
    assert len(data.orders) >= 2000
    assert len(data.products) == len(CATALOG) == 60


def test_status_shares_are_close_to_the_plan(data: SeedData) -> None:
    counts = Counter(order.status for order in data.orders[:2000])
    share = {status: counts[status] / 2000 for status in OrderStatus}
    assert 0.68 <= share[OrderStatus.DELIVERED] <= 0.78
    assert 0.09 <= share[OrderStatus.SHIPPED] <= 0.15
    assert 0.04 <= share[OrderStatus.PROCESSING] <= 0.08
    assert 0.015 <= share[OrderStatus.PENDING] <= 0.05
    assert 0.015 <= share[OrderStatus.CANCELLED] <= 0.05


def test_totals_add_up_and_prices_come_from_the_catalog(data: SeedData) -> None:
    for order in data.orders:
        subtotal = sum(line.unit_price_cents * line.quantity for line in order.lines)
        assert order.total_cents == subtotal + order.shipping_cents
        for line in order.lines:
            assert line.unit_price_cents == BY_SKU[line.sku].price_cents


def test_emails_are_unique_reserved_domain_and_personas_present(data: SeedData) -> None:
    emails = [customer.email for customer in data.customers]
    assert len(emails) == len(set(emails))
    assert all(email.split("@")[1].startswith("example.") for email in emails)
    assert set(emails) >= PERSONA_EMAILS


def test_order_numbers_stay_out_of_the_fixture_range(data: SeedData) -> None:
    for order in data.orders:
        number = int(order.number.removeprefix("PV-"))
        assert number < 900000, "PV-9xxxxx is reserved for eval fixtures"
