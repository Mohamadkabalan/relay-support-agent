"""ORM models for the store and its operations tables (PLAN.md §8).

Conventions:

- Money is integer cents; CHECK constraints keep amounts non-negative and quantities positive.
- Enumerations are text columns with a CHECK constraint built from the Python enum.
- Customer-facing entities carry a readable number (PV-, RMA-, RF-, APR-, ESC-) next to the UUID.
- `conversation_id` columns on store, approval and audit rows are plain UUIDs, not foreign keys:
  the audit trail has to outlive a conversation (the demo reset deletes conversations, and audit
  rows can't be changed), and the MCP server records whatever conversation the API's signed turn
  token names. The conversation tables themselves use real foreign keys.
- Transcript payloads (`messages.raw`, `conversations.tool_manifest`) are text checked with
  `IS JSON`, not `jsonb`: replay must send back the exact bytes, and `jsonb` reorders keys.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Sequence,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from relay.db.base import Base, TextEnum, uuid_pk
from relay.domain.enums import (
    ActorType,
    ApprovalStatus,
    ConversationStatus,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    InboxKind,
    MessageKind,
    OrderStatus,
    PreviewOutcome,
    ProductCondition,
    ProductKind,
    RefundChannel,
    RefundLineKind,
    RefundReason,
    RefundStatus,
    ReturnReason,
    ReturnStatus,
    ShipmentStatus,
    ToolCallOutcome,
    TurnStatus,
    TurnTrigger,
    WriteAction,
)

EMBEDDING_DIMENSIONS = 384  # BAAI/bge-small-en-v1.5

# Readable numbers for customer-facing records. The seed numbers its own rows from these starting
# values and then moves each sequence past them.
RMA_NUMBERS = Sequence("rma_number_seq", start=100001, metadata=Base.metadata)
REFUND_NUMBERS = Sequence("refund_number_seq", start=10001, metadata=Base.metadata)
APPROVAL_NUMBERS = Sequence("approval_number_seq", start=10001, metadata=Base.metadata)
ESCALATION_NUMBERS = Sequence("escalation_number_seq", start=10001, metadata=Base.metadata)
# Orders of the per-visitor demo persona clones (D4): PV-801000 to PV-899999. The seed's random
# orders use PV-100000 to PV-799999, the personas PV-8001xx to PV-8006xx, eval fixtures PV-9xxxxx.
DEMO_ORDER_NUMBERS = Sequence(
    "demo_order_number_seq", start=801000, maxvalue=899999, metadata=Base.metadata
)


def one_of(column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")


def non_negative(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} >= 0", name=f"{column}_non_negative")


def positive(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} > 0", name=f"{column}_positive")


def matches(column: str, pattern: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '{pattern}'", name=f"{column}_format")


# ---------------------------------------------------------------- store


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    demo_visitor_id: Mapped[str | None] = mapped_column(Text, index=True)

    addresses: Mapped[list[Address]] = relationship(back_populates="customer")


class Address(Base):
    __tablename__ = "addresses"
    __table_args__ = (
        Index(
            "uq_addresses_one_default",
            "customer_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    recipient_name: Mapped[str] = mapped_column(Text)
    line1: Mapped[str] = mapped_column(Text)
    line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    postal_code: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, server_default="US")
    is_default: Mapped[bool] = mapped_column(server_default=text("false"))

    customer: Mapped[Customer] = relationship(back_populates="addresses")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        one_of("kind", ProductKind),
        one_of("condition", ProductCondition),
        non_negative("price_cents"),
    )

    sku: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    kind: Mapped[ProductKind] = mapped_column(TextEnum(ProductKind))
    condition: Mapped[ProductCondition] = mapped_column(TextEnum(ProductCondition))
    price_cents: Mapped[int] = mapped_column(Integer)
    final_sale: Mapped[bool] = mapped_column(server_default=text("false"))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        matches("number", r"^PV-[0-9]{6}$"),
        matches("payment_last4", r"^[0-9]{4}$"),
        one_of("status", OrderStatus),
        non_negative("shipping_cents"),
        non_negative("total_cents"),
        Index("ix_orders_customer_placed", "customer_id", "placed_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[str] = mapped_column(Text, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[OrderStatus] = mapped_column(TextEnum(OrderStatus))
    placed_at: Mapped[datetime]
    promised_delivery_on: Mapped[date | None]
    shipping_address: Mapped[dict[str, Any]]  # a snapshot, so later profile edits don't move it
    payment_brand: Mapped[str] = mapped_column(Text)
    payment_last4: Mapped[str] = mapped_column(Text)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)  # lines plus shipping, tax included
    customer_note: Mapped[str | None] = mapped_column(Text)  # untrusted: written by the customer
    cancelled_at: Mapped[datetime | None]

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", order_by="OrderLine.line_no"
    )
    shipment: Mapped[Shipment | None] = relationship(back_populates="order")


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no"),
        positive("line_no"),
        positive("quantity"),
        non_negative("unit_price_cents"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    name: Mapped[str] = mapped_column(Text)  # as sold
    unit_price_cents: Mapped[int] = mapped_column(Integer)  # as sold
    quantity: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()


class Shipment(Base):
    """One shipment per order in v1 (the unique order_id keeps the lost-package rule simple)."""

    __tablename__ = "shipments"
    __table_args__ = (one_of("status", ShipmentStatus),)

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), unique=True)
    carrier: Mapped[str] = mapped_column(Text)
    tracking_number: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[ShipmentStatus] = mapped_column(TextEnum(ShipmentStatus))
    shipped_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    eta: Mapped[date | None]

    order: Mapped[Order] = relationship(back_populates="shipment")
    events: Mapped[list[ShipmentEvent]] = relationship(
        back_populates="shipment", order_by="ShipmentEvent.occurred_at"
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"
    __table_args__ = (Index("ix_shipment_events_shipment_time", "shipment_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipments.id"))
    occurred_at: Mapped[datetime]
    code: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)  # untrusted: carrier text
    location: Mapped[str | None] = mapped_column(Text)

    shipment: Mapped[Shipment] = relationship(back_populates="events")


class Return(Base):
    __tablename__ = "returns"
    __table_args__ = (
        matches("rma_number", r"^RMA-[0-9]{6}$"),
        one_of("status", ReturnStatus),
        one_of("reason", ReturnReason),
        one_of("created_by_type", ActorType),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    rma_number: Mapped[str] = mapped_column(Text, unique=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[ReturnStatus] = mapped_column(TextEnum(ReturnStatus))
    reason: Mapped[ReturnReason] = mapped_column(TextEnum(ReturnReason))
    details: Mapped[str | None] = mapped_column(Text)
    label_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    created_by_type: Mapped[ActorType] = mapped_column(TextEnum(ActorType))
    preview_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("action_previews.id"), unique=True
    )
    conversation_id: Mapped[uuid.UUID | None]

    lines: Mapped[list[ReturnLine]] = relationship(back_populates="return_")


class ReturnLine(Base):
    __tablename__ = "return_lines"
    __table_args__ = (UniqueConstraint("return_id", "order_line_id"), positive("quantity"))

    id: Mapped[uuid.UUID] = uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("returns.id"))
    order_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_lines.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)

    return_: Mapped[Return] = relationship(back_populates="lines")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        matches("refund_number", r"^RF-[0-9]{5,}$"),
        one_of("reason", RefundReason),
        one_of("channel", RefundChannel),
        one_of("status", RefundStatus),
        positive("amount_cents"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    refund_number: Mapped[str] = mapped_column(Text, unique=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    reason: Mapped[RefundReason] = mapped_column(TextEnum(RefundReason))
    channel: Mapped[RefundChannel] = mapped_column(TextEnum(RefundChannel))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[RefundStatus] = mapped_column(TextEnum(RefundStatus))
    issued_at: Mapped[datetime]
    # One refund per preview: a retried commit can never pay twice.
    preview_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("action_previews.id"), unique=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    return_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("returns.id"))
    conversation_id: Mapped[uuid.UUID | None]

    lines: Mapped[list[RefundLine]] = relationship(back_populates="refund")


class RefundLine(Base):
    __tablename__ = "refund_lines"
    __table_args__ = (
        one_of("kind", RefundLineKind),
        positive("amount_cents"),
        CheckConstraint(
            "(kind = 'item') = (order_line_id IS NOT NULL AND quantity IS NOT NULL)",
            name="item_lines_have_quantity",
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    refund_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("refunds.id"))
    kind: Mapped[RefundLineKind] = mapped_column(TextEnum(RefundLineKind))
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("order_lines.id"), index=True
    )
    quantity: Mapped[int | None] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)

    refund: Mapped[Refund] = relationship(back_populates="lines")


# ---------------------------------------------------------------- operations


class Operator(Base):
    __tablename__ = "operators"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ActionPreview(Base):
    """A preview of a write, and the single-use token that confirms it (D1, CNF-1)."""

    __tablename__ = "action_previews"
    __table_args__ = (
        one_of("action", WriteAction),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('completed', 'pending_approval', 'denied')",
            name="outcome_valid",
        ),
        CheckConstraint("(committed_at IS NULL) = (outcome IS NULL)", name="committed_has_outcome"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token: Mapped[str] = mapped_column(Text, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    conversation_id: Mapped[uuid.UUID | None]
    issued_turn_seq: Mapped[int | None] = mapped_column(Integer)  # None over stdio
    action: Mapped[WriteAction] = mapped_column(TextEnum(WriteAction))
    canonical_args: Mapped[dict[str, Any]]
    args_hash: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    needs_approval: Mapped[bool]
    policy_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    committed_at: Mapped[datetime | None]
    outcome: Mapped[PreviewOutcome | None] = mapped_column(TextEnum(PreviewOutcome))
    result: Mapped[dict[str, Any] | None]  # replayed when a commit is retried


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        matches("approval_number", r"^APR-[0-9]{5,}$"),
        one_of("action", WriteAction),
        one_of("status", ApprovalStatus),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided_when_final"),
        Index(
            "ix_approvals_pending",
            "requested_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    approval_number: Mapped[str] = mapped_column(Text, unique=True)
    preview_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("action_previews.id"), unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    conversation_id: Mapped[uuid.UUID | None]
    action: Mapped[WriteAction] = mapped_column(TextEnum(WriteAction))
    payload: Mapped[dict[str, Any]]  # the preview's canonical arguments
    summary: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    rules: Mapped[list[str]] = mapped_column(ARRAY(Text))  # the approval triggers that matched
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[ApprovalStatus] = mapped_column(TextEnum(ApprovalStatus))
    policy_version: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))
    note: Mapped[str | None] = mapped_column(Text)
    execution_result: Mapped[dict[str, Any] | None]


class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (
        matches("escalation_number", r"^ESC-[0-9]{5,}$"),
        one_of("reason", EscalationReason),
        one_of("priority", EscalationPriority),
        one_of("status", EscalationStatus),
        Index(
            "uq_escalations_one_open_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    escalation_number: Mapped[str] = mapped_column(Text, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None]
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    reason: Mapped[EscalationReason] = mapped_column(TextEnum(EscalationReason))
    summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[EscalationPriority] = mapped_column(TextEnum(EscalationPriority))
    status: Mapped[EscalationStatus] = mapped_column(TextEnum(EscalationStatus))
    created_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operators.id"))


class AuditLog(Base):
    """Append-only: a trigger rejects UPDATE and DELETE (see the baseline migration)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        one_of("actor_type", ActorType),
        Index("ix_audit_log_entity", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime]
    actor_type: Mapped[ActorType] = mapped_column(TextEnum(ActorType))
    actor_id: Mapped[str] = mapped_column(Text)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(index=True)  # the customer
    action: Mapped[str] = mapped_column(Text)  # "refund.issue"
    entity: Mapped[str] = mapped_column(Text)  # "refund"
    entity_id: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]
    reason: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[uuid.UUID | None]
    conversation_id: Mapped[uuid.UUID | None]
    trace_id: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- conversations (agent runtime)


def is_json(column: str, *, nullable: bool = False) -> CheckConstraint:
    condition = f"{column} IS JSON"
    if nullable:
        condition = f"{column} IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"{column}_is_json")


class Conversation(Base):
    """One chat between a customer and the agent, with everything that is pinned for its lifetime:
    the provider and model, the system prompt version and the tool manifest (ADR-0005)."""

    __tablename__ = "conversations"
    __table_args__ = (
        one_of("status", ConversationStatus),
        is_json("tool_manifest"),
        non_negative("cost_micros"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[ConversationStatus] = mapped_column(TextEnum(ConversationStatus))
    resolution: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    prompt_hash: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)  # the exact text every request sends
    tool_manifest: Mapped[str] = mapped_column(Text)  # exact JSON text, see the module docstring
    manifest_hash: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)  # millionths of a dollar
    cost_complete: Mapped[bool] = mapped_column(default=True)  # False if a model had no price
    injection_flags: Mapped[int] = mapped_column(Integer, default=0)
    budget_warned: Mapped[bool] = mapped_column(default=False)
    flags: Mapped[list[Any]] = mapped_column(default=list)  # for review: retractions, refusals
    rating: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]

    @property
    def processed_tokens(self) -> int:
        """Every token the conversation has made a model read or write (the §5.7 budget)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        one_of("trigger", TurnTrigger),
        one_of("status", TurnStatus),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished_when_final"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[TurnTrigger] = mapped_column(TextEnum(TurnTrigger))
    status: Mapped[TurnStatus] = mapped_column(TextEnum(TurnStatus))
    steps: Mapped[int] = mapped_column(Integer, default=0)  # model calls
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retracted: Mapped[bool] = mapped_column(default=False)  # an output guard replaced the reply
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class Message(Base):
    """The transcript. Append-only: a trigger rejects UPDATE and DELETE (ADR-0005).

    `raw` holds the exact provider payload (a JSON array of provider messages) that is replayed on
    every later request; it is NULL for notices, which the model never sees. `content` is the same
    entry in a provider-neutral form for the console and analytics.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        one_of("kind", MessageKind),
        is_json("raw", nullable=True),
        CheckConstraint("(kind = 'notice') = (raw IS NULL)", name="raw_unless_notice"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turns.id"), index=True)
    kind: Mapped[MessageKind] = mapped_column(TextEnum(MessageKind))
    content: Mapped[dict[str, Any]]
    raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (UniqueConstraint("turn_id", "seq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"))
    seq: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(Text)
    requested_model: Mapped[str] = mapped_column(Text)
    served_model: Mapped[str] = mapped_column(Text)
    effort: Mapped[str | None] = mapped_column(Text)
    max_tokens: Mapped[int] = mapped_column(Integer)
    stop_reason: Mapped[str] = mapped_column(Text)
    refusal_category: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool]
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)  # None when the model has no price
    latency_ms: Mapped[int] = mapped_column(Integer)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(Text)
    input_transformations: Mapped[list[Any] | None]
    thinking: Mapped[str | None] = mapped_column(Text)  # summarized, for the operator replay
    created_at: Mapped[datetime]


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (UniqueConstraint("turn_id", "seq"), one_of("outcome", ToolCallOutcome))

    id: Mapped[uuid.UUID] = uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"))
    seq: Mapped[int] = mapped_column(Integer)
    tool_use_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, Any] | None]
    result: Mapped[str] = mapped_column(Text)  # exactly what the model received
    is_error: Mapped[bool]
    outcome: Mapped[ToolCallOutcome] = mapped_column(TextEnum(ToolCallOutcome))
    error_code: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime]


class InboxItem(Base):
    """Customer messages and events waiting for the next turn. A turn claims them in order."""

    __tablename__ = "conversation_inbox"
    __table_args__ = (
        one_of("kind", InboxKind),
        Index(
            "ix_conversation_inbox_unclaimed",
            "conversation_id",
            "id",
            postgresql_where=text("turn_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    kind: Mapped[InboxKind] = mapped_column(TextEnum(InboxKind))
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turns.id"))  # claimed by


class ConversationEvent(Base):
    """Events for the chat stream (§5.10), kept for reconnects. Text deltas are not stored."""

    __tablename__ = "conversation_events"
    __table_args__ = (Index("ix_conversation_events_conversation_id_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turns.id"))
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


# ---------------------------------------------------------------- knowledge base


class HelpArticle(Base):
    __tablename__ = "help_articles"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)  # Markdown, policy placeholders already rendered
    policy_version: Mapped[str] = mapped_column(Text)

    chunks: Mapped[list[KbChunk]] = relationship(
        back_populates="article", order_by="KbChunk.ordinal"
    )


class KbChunk(Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("article_id", "ordinal"),
        Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_kb_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("help_articles.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text)  # "Article title > Section"
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', heading), 'A') || "
            "setweight(to_tsvector('english', text), 'B')",
            persisted=True,
        ),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    article: Mapped[HelpArticle] = relationship(back_populates="chunks")


class SeedRun(Base):
    """What `relay seed` wrote, so a second run can tell whether the data is already there."""

    __tablename__ = "seed_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    seed: Mapped[int] = mapped_column(Integer)
    seeded_now: Mapped[datetime]
    generator_version: Mapped[str] = mapped_column(Text)
    data_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
