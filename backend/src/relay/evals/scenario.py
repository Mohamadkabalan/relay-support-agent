"""The scenario format (PLAN.md §9.2): fixtures, a simulated customer, an operator, expectations.

Scenarios are YAML files under `evals/scenarios/` and `evals/redteam/`. Every entity a scenario
talks about is created by its own fixtures, so a scenario never depends on the base seed beyond
the catalog and the help center. Strings may refer to fixture values with `{ref.field}`, for
example `{o1.number}` or `{me.name}`; they are filled in once the fixtures exist.

`load_scenarios` parses and validates every file: unknown keys, dangling references and unknown
`{ref.field}` placeholders are errors, so a typo fails `make eval-selftest` instead of a run.
"""

import hashlib
import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay.evals.snapshot import TABLES
from relay.seed.catalog import BY_SKU

PLACEHOLDER = re.compile(r"\{(\w+)\.(\w+)\}")
CUSTOMER_FIELDS = {"name", "email", "first_name"}
ORDER_FIELDS = {"number", "tracking_number", "carrier"}


class Category(StrEnum):
    HAPPY_PATH = "happy_path"
    POLICY_EDGE = "policy_edge"
    MULTIPLE_REQUESTS = "multiple_requests"
    CHANGE_OF_MIND = "change_of_mind"
    UPSET_CUSTOMER = "upset_customer"
    NOT_ENTITLED = "not_entitled"
    OTHER_CUSTOMERS = "other_customers"
    HITL = "hitl"
    ESCALATION = "escalation"
    RED_TEAM = "red_team"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"


OrderKind = Literal[
    "pending",
    "processing",
    "label_created",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "lost",
    "cancelled",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerFixture(_Strict):
    ref: str
    name: str
    email: str | None = None  # default: <ref>.<scenario>@eval.example, unique per scenario


class LineFixture(_Strict):
    sku: str
    quantity: int = Field(default=1, ge=1)
    unit_price_cents: int | None = Field(default=None, ge=1)  # a sale price; default: catalog


class CarrierEvent(_Strict):
    """A scan added to the carrier's history, for example one with planted text."""

    days_ago: float
    code: str = "in_transit"
    description: str
    location: str | None = None


class LineQuantity(_Strict):
    line: int = Field(ge=1)  # the order line's number, from 1
    quantity: int = Field(default=1, ge=1)


class PriorReturn(_Strict):
    """A return the customer started before the trial, for the quantity and velocity rules."""

    status: Literal["label_issued", "in_transit", "received", "completed", "cancelled"]
    reason: str = "changed_mind"
    days_ago: int = 3
    lines: list[LineQuantity] = Field(min_length=1)


class PriorRefund(_Strict):
    """A refund issued before the trial: `channel: agent` ones count toward the velocity rule."""

    reason: Literal["damaged_or_defective", "lost_in_transit", "late_delivery", "return_received"]
    channel: Literal["agent", "staff"] = "agent"
    days_ago: int = 10
    lines: list[LineQuantity] = []
    shipping: bool = False

    @model_validator(mode="after")
    def _refunds_something(self) -> Self:
        if not self.lines and not self.shipping:
            raise ValueError("a prior refund needs lines or shipping: true")
        return self


class ArticleFixture(_Strict):
    """A help article added to the knowledge base, for example one with a planted instruction."""

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    title: str
    category: str = "returns"
    body: str


class OrderFixture(_Strict):
    ref: str
    customer: str
    number: str | None = None  # default: PV-9xxxxx, in fixture order
    status: OrderKind = "delivered"
    lines: list[LineFixture] = Field(min_length=1)
    delivered_days_ago: int = 5  # delivered orders
    late_by_days: int = 0  # delivered orders: days after the promised date
    last_scan_days_ago: float = 1  # in transit and lost packages
    shipping_cents: int = 599
    customer_note: str | None = None
    carrier_events: list[CarrierEvent] = []
    returns: list[PriorReturn] = []
    refunds: list[PriorRefund] = []

    @model_validator(mode="after")
    def _lines_exist(self) -> Self:
        priors: list[PriorReturn | PriorRefund] = [*self.returns, *self.refunds]
        for prior in priors:
            for item in prior.lines:
                if item.line > len(self.lines):
                    raise ValueError(f"order {self.ref} has no line {item.line}")
        return self


class Fixtures(_Strict):
    customers: list[CustomerFixture] = Field(min_length=1)
    orders: list[OrderFixture] = []
    articles: list[ArticleFixture] = []


class SimulatedCustomer(_Strict):
    persona: str = ""
    goal: str
    known_facts: list[str] = []
    hidden_facts: list[str] = []  # revealed only when the agent asks
    constraints: list[str] = []
    first_message: str | None = None
    # Red team: scripted opening messages, then up to `adaptive_followups` messages from an
    # attacker model that adapts to the agent's replies.
    script: list[str] = []
    adaptive_followups: int = 0
    max_turns: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _has_an_opening(self) -> Self:
        if (self.first_message is None) == (not self.script):
            raise ValueError("give exactly one of first_message or script")
        return self

    @property
    def opening(self) -> str:
        return self.first_message if self.first_message is not None else self.script[0]


class Operator(_Strict):
    """What the operator stub does with an approval the agent requests (§9.1).

    `expire` moves the trial's clock past the approval's deadline and runs the expiry job, which
    expires it and opens an escalation. `after_customer_messages` holds the decision until the
    customer has sent that many more messages, for a customer who keeps chatting while waiting.
    """

    decision: Literal["approve", "reject", "expire", "leave_pending"] = "leave_pending"
    note: str | None = None
    after_customer_messages: int = Field(default=0, ge=0)


class Check(_Strict):
    """One deterministic conversation check on what the customer saw (§9.3)."""

    assistant_mentions: str | None = None  # case-insensitive substring, may use {ref.field}
    assistant_mentions_amount_cents: int | None = None  # "$349.99"
    # A value of a row the trial created, for example the RMA number of the new return.
    assistant_mentions_new: (
        Literal[
            "returns.rma_number",
            "refunds.refund_number",
            "approvals.approval_number",
            "escalations.escalation_number",
        ]
        | None
    ) = None
    assistant_never_mentions: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [name for name, value in self if value is not None]
        if len(given) != 1:
            raise ValueError(f"a conversation check has exactly one key, got {given or 'none'}")
        return self

    @property
    def positive(self) -> bool:
        """Whether a do-nothing agent could pass it."""
        return self.assistant_never_mentions is None


class Expect(_Strict):
    # Table -> the rows the trial must insert or change, as partial matches. Every other row of
    # every snapshotted table must stay unchanged (§9.3).
    db: dict[str, list[dict[str, Any]]] = {}
    escalated: bool = False
    # Escalating is acceptable but not required (a red-team attack is fair to hand to a person):
    # an escalation is then neither missing nor unexpected.
    may_escalate: bool = False
    conversation: list[Check] = []
    turn_limit_ok: bool = False  # whether stopping at the customer-turn limit is acceptable
    expected_tool_errors: list[str] = []  # error codes this scenario provokes on purpose
    violations: Literal["none"] = "none"

    @model_validator(mode="after")
    def _known_tables(self) -> Self:
        unknown = set(self.db) - set(TABLES)
        if unknown:
            raise ValueError(f"expect.db names unknown tables {sorted(unknown)}; use {TABLES}")
        return self


class ToolStep(_Strict):
    tool: str
    args: dict[str, Any] = {}


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    title: str
    category: Category
    tags: list[str] = []
    subsets: list[str] = []  # named groups, such as v1 (the Phase 4 scenarios)
    ci: bool = False
    severity: Severity | None = None  # red team only
    attack: str | None = None  # red team only
    fixtures: Fixtures
    session_customer: str
    customer: SimulatedCustomer
    operator: Operator = Operator()
    expect: Expect
    judge: bool = True  # whether the judge (§9.6) scores this scenario's trials
    # The right actions, done directly: the oracle self-test applies them and the grader must
    # pass. In a scenario where nothing may change, `tempting` (optional) is the write the
    # customer pushes for; the policy-agreement self-test checks that the server refuses it.
    oracle: list[ToolStep] = []
    tempting: ToolStep | None = None
    source: str = ""  # the file it came from, filled in by the loader

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        refs = [c.ref for c in self.fixtures.customers] + [o.ref for o in self.fixtures.orders]
        if len(refs) != len(set(refs)):
            raise ValueError("fixture refs must be unique")
        customers = {c.ref for c in self.fixtures.customers}
        if self.session_customer not in customers:
            raise ValueError(f"session_customer {self.session_customer!r} is not a fixture")
        for order in self.fixtures.orders:
            if order.customer not in customers:
                raise ValueError(f"order {order.ref} names unknown customer {order.customer!r}")
            for line in order.lines:
                if line.sku not in BY_SKU:
                    raise ValueError(f"order {order.ref}: unknown SKU {line.sku}")
        red_team = self.category is Category.RED_TEAM
        if red_team != (self.severity is not None):
            raise ValueError("severity is required for red-team scenarios and only for them")
        if self.expect.may_escalate and (self.expect.escalated or "escalations" in self.expect.db):
            raise ValueError("may_escalate is for scenarios that don't expect an escalation")
        if self.expect.db and not self.oracle:
            raise ValueError("a scenario that expects changes needs oracle steps")
        if self.tempting is not None and self.expect.db:
            raise ValueError("tempting is for scenarios where nothing may change")
        self._check_placeholders(refs)
        return self

    def _check_placeholders(self, refs: list[str]) -> None:
        orders = {o.ref for o in self.fixtures.orders}
        for text in _strings(self.model_dump(exclude={"source"})):
            for ref, field in PLACEHOLDER.findall(text):
                if ref not in refs:
                    raise ValueError(f"{{{ref}.{field}}}: no fixture {ref!r}")
                allowed = ORDER_FIELDS if ref in orders else CUSTOMER_FIELDS
                if field not in allowed:
                    raise ValueError(f"{{{ref}.{field}}}: use one of {sorted(allowed)}")

    @property
    def expects_action(self) -> bool:
        """Whether the right outcome changes something: a write or an escalation."""
        return bool(self.expect.db) or self.expect.escalated

    @property
    def red_team(self) -> bool:
        return self.category is Category.RED_TEAM


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def fill(value: Any, values: dict[str, dict[str, str]]) -> Any:
    """Replace `{ref.field}` placeholders, recursively, with the fixture values."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: values[m.group(1)][m.group(2)], value)
    if isinstance(value, dict):
        return {key: fill(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, values) for item in value]
    return value


class ScenarioError(ValueError):
    pass


def load_scenarios(directories: Iterable[Path]) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for directory in directories:
        for path in sorted(directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text("utf-8"))
                scenario = Scenario.model_validate({**data, "source": str(path)})
            except (yaml.YAMLError, ValueError) as error:
                raise ScenarioError(f"{path}: {error}") from error
            if scenario.id != path.stem:
                raise ScenarioError(f"{path}: the id must match the file name ({path.stem})")
            scenarios.append(scenario)
    ids = [scenario.id for scenario in scenarios]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ScenarioError(f"duplicate scenario ids: {sorted(duplicates)}")
    return scenarios


def select(
    scenarios: list[Scenario], *, subset: str | None = None, ids: list[str] | None = None
) -> list[Scenario]:
    chosen = scenarios
    if subset == "ci":
        chosen = [s for s in chosen if s.ci]
    elif subset == "regular":
        chosen = [s for s in chosen if not s.red_team]
    elif subset == "red_team":
        chosen = [s for s in chosen if s.red_team]
    elif subset:
        chosen = [s for s in chosen if subset in s.subsets]
    if ids:
        unknown = set(ids) - {s.id for s in scenarios}
        if unknown:
            raise ScenarioError(f"unknown scenario ids: {sorted(unknown)}")
        chosen = [s for s in chosen if s.id in ids]
    return chosen


def suite_hash(scenarios: list[Scenario]) -> str:
    digest = hashlib.sha256()
    for scenario in sorted(scenarios, key=lambda s: s.id):
        digest.update(scenario.model_dump_json(exclude={"source"}).encode())
    return digest.hexdigest()[:16]
