"""Small domain building blocks: money, the clock, addresses, the policy file, preview hashing."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from relay.clock import FrozenClock, SystemClock, local_date
from relay.domain.address import Address
from relay.domain.errors import ConfirmationTooEarlyError, OrderNotFoundError
from relay.domain.money import format_cents
from relay.domain.policy_config import default_policy, load_policy
from relay.domain.previews import args_hash

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "$0.00"),
        (5, "$0.05"),
        (34999, "$349.99"),
        (10000, "$100.00"),
        (249999, "$2,499.99"),
        (-500, "-$5.00"),
    ],
)
def test_format_cents(cents: int, text: str) -> None:
    assert format_cents(cents) == text


def test_frozen_clock_moves_only_when_told() -> None:
    start = datetime(2026, 9, 15, 15, 0, tzinfo=NY)
    clock = FrozenClock(start)
    assert clock.now() == start
    clock.advance(timedelta(minutes=31))
    assert clock.now() == start + timedelta(minutes=31)
    clock.set(start)
    assert clock.now() == start


def test_clocks_refuse_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 9, 15, 15, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        local_date(datetime(2026, 9, 15, 15, 0), NY)
    assert SystemClock().now().tzinfo is UTC


def test_local_date_uses_the_store_time_zone() -> None:
    late_evening = datetime(2026, 8, 16, 3, 30, tzinfo=UTC)  # 23:30 on Aug 15 in New York
    assert local_date(late_evening, NY) == date(2026, 8, 15)


def test_address_normalizes_whitespace_and_state_case() -> None:
    address = Address(
        recipient_name="  Dana   Whitfield ",
        line1="418  Maple Avenue",
        line2=" ",
        city="Columbus",
        state="oh",
        postal_code="43215",
    )
    normalized = address.normalized()
    assert normalized.recipient_name == "Dana Whitfield"
    assert normalized.line1 == "418 Maple Avenue"
    assert normalized.line2 is None
    assert normalized.state == "OH"
    assert normalized.one_line() == "Dana Whitfield, 418 Maple Avenue, Columbus, OH 43215"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "ZZ", "not a US state code"),
        ("postal_code", "4321", "not a ZIP code"),
        ("line1", "", "at least 1 character"),
        ("line1", "x" * 101, "at most 100 characters"),
    ],
)
def test_us_addresses_are_validated(field: str, value: str, message: str) -> None:
    fields = {
        "recipient_name": "Dana Whitfield",
        "line1": "418 Maple Avenue",
        "city": "Columbus",
        "state": "OH",
        "postal_code": "43215",
        field: value,
    }
    with pytest.raises(ValidationError, match=message):
        Address.model_validate(fields)


def test_foreign_addresses_pass_validation_so_the_policy_can_explain() -> None:
    address = Address(
        recipient_name="Jo Tremblay",
        line1="1 Rue Sainte-Catherine",
        city="Montreal",
        state="Quebec",
        postal_code="H2X 1Z4",
        country="ca",
    )
    assert address.country == "CA"
    assert address.one_line().endswith(", CA")


def test_packaged_policy_loads_and_resolves_placeholders() -> None:
    policy = default_policy()
    assert policy.lookup("returns.window_days") == 30
    assert policy.lookup("refunds.auto_approve_limit_cents") == 10000
    assert policy.tz == NY
    for missing in ("returns.nope", "returns", "nope.window_days"):
        with pytest.raises(KeyError):
            policy.lookup(missing)


def test_policy_file_rejects_unknown_keys(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "src/relay/domain/policy.yaml"
    broken = tmp_path / "policy.yaml"
    broken.write_text(source.read_text() + "surprise: true\n")
    with pytest.raises(ValidationError, match="surprise"):
        load_policy(broken)


def test_args_hash_ignores_key_order() -> None:
    first = {"order_number": "PV-100001", "lines": {"1": 1, "2": 3}}
    second = {"lines": {"2": 3, "1": 1}, "order_number": "PV-100001"}
    assert args_hash(first) == args_hash(second)
    assert args_hash(first) != args_hash({**first, "lines": {"1": 2, "2": 3}})


def test_errors_render_as_tool_error_text() -> None:
    assert str(OrderNotFoundError("PV-104299")) == (
        "[ORDER_NOT_FOUND] No order PV-104299 on this account. "
        "Ask the customer to check the number, or call list_orders."
    )
    assert str(ConfirmationTooEarlyError()).startswith("[CONFIRMATION_TOO_EARLY] ")
