"""What the services return: read views and write outcomes.

Write outcomes carry a `status`: `preview`, `completed`, `pending_approval` or `denied` (§6.2).
They are Pydantic models because a committed outcome is stored on its preview and replayed,
byte for byte, when a commit is retried.

Free text written by customers or carriers (order notes, tracking events) is returned as plain
strings here; the MCP layer wraps it as untrusted text before the model sees it.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from relay.domain.address import Address
from relay.domain.enums import (
    ApprovalStatus,
    EscalationPriority,
    EscalationStatus,
    OrderStatus,
    RefundReason,
    ReturnReason,
    ReturnStatus,
    ShipmentStatus,
    WriteAction,
)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- reads


class CustomerProfile(_View):
    full_name: str
    email: str
    member_since: date
    default_address: Address | None
    open_returns: int
    open_requests: int  # pending approvals plus open escalations


class OrderSummary(_View):
    number: str
    placed_on: date
    status: OrderStatus
    total_cents: int
    item_count: int
    first_items: list[str]
    shipment_status: ShipmentStatus | None


class OrderLineView(_View):
    line: int
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    refundable_quantity: int
    refundable_cents: int
    return_window_ends_on: date | None
    final_sale: bool
    condition: str


class ShipmentSummary(_View):
    carrier: str
    tracking_number: str
    status: ShipmentStatus
    shipped_at: datetime | None
    delivered_at: datetime | None
    eta: date | None


class ReturnView(_View):
    rma_number: str
    status: ReturnStatus
    reason: ReturnReason
    created_at: datetime
    lines: dict[int, int]  # line number -> quantity


class RefundView(_View):
    refund_number: str
    reason: RefundReason
    amount_cents: int
    issued_at: datetime


class PendingRequestView(_View):
    approval_number: str
    action: WriteAction
    summary: str
    requested_at: datetime


class AvailabilityView(_View):
    allowed: bool
    needs_approval: bool = False
    code: str | None = None
    window_ends_on: date | None = None
    lines: list[int] = []


class AllowedActionsView(_View):
    return_: AvailabilityView
    refund: dict[RefundReason, AvailabilityView]
    change_address: AvailabilityView


class OrderDetail(_View):
    number: str
    status: OrderStatus
    placed_at: datetime
    promised_delivery_on: date | None
    lines: list[OrderLineView]
    shipping_cents: int
    total_cents: int
    refunded_cents: int
    payment: str  # "Visa ending 4242"
    shipping_address: Address
    shipment: ShipmentSummary | None
    returns: list[ReturnView]
    refunds: list[RefundView]
    pending_requests: list[PendingRequestView]
    customer_note: str | None  # untrusted: written by the customer
    allowed_actions: AllowedActionsView


class TrackingEvent(_View):
    occurred_at: datetime
    code: str
    description: str  # untrusted: carrier text
    location: str | None


class Tracking(_View):
    order_number: str
    carrier: str
    tracking_number: str
    status: ShipmentStatus
    eta: date | None
    delivered_at: datetime | None
    days_since_last_scan: int | None
    stalled: bool  # no scan for long enough that a lost-package refund applies
    events: list[TrackingEvent]


# ---------------------------------------------------------------- writes


class PreviewResult(_View):
    status: Literal["preview"] = "preview"
    confirmation_token: str
    expires_at: datetime
    summary: str
    amount_cents: int | None
    needs_operator_approval: bool
    approval_reason: str | None


class DeniedResult(_View):
    status: Literal["denied"] = "denied"
    rule: str
    code: str
    explanation: str
    alternatives: list[str]


class PendingApprovalResult(_View):
    status: Literal["pending_approval"] = "pending_approval"
    approval_number: str
    summary: str
    expected_decision: str


class RefundCompleted(_View):
    status: Literal["completed"] = "completed"
    refund_number: str
    amount_cents: int
    destination: str
    settles_in: str


class ReturnCompleted(_View):
    status: Literal["completed"] = "completed"
    rma_number: str
    label_url: str
    instructions: str
    refund_estimate_cents: int


class AddressChangeCompleted(_View):
    status: Literal["completed"] = "completed"
    order_number: str
    shipping_address: Address


Completed = RefundCompleted | ReturnCompleted | AddressChangeCompleted
WriteResult = PreviewResult | DeniedResult | PendingApprovalResult | Completed


class EscalationResult(_View):
    escalation_number: str
    status: EscalationStatus
    priority: EscalationPriority
    expected_response: str
    already_open: bool


class ApprovalView(_View):
    approval_number: str
    status: ApprovalStatus
    action: WriteAction
    order_number: str
    summary: str
    amount_cents: int | None
    rules: list[str]
    reason: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    note: str | None
    outcome: str | None  # what happened when it was executed or failed
