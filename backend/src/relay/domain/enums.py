"""The fixed vocabularies of the store.

They are stored as plain text in Postgres with CHECK constraints (see `relay.db.models`), so adding
a value is a migration, never a silent change.
"""

from enum import StrEnum


class ProductKind(StrEnum):
    PHYSICAL = "physical"
    GIFT_CARD = "gift_card"
    DIGITAL = "digital"


class ProductCondition(StrEnum):
    NEW = "new"
    REFURBISHED = "refurbished"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class ShipmentStatus(StrEnum):
    LABEL_CREATED = "label_created"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    LOST = "lost"


class ReturnReason(StrEnum):
    CHANGED_MIND = "changed_mind"
    NO_LONGER_NEEDED = "no_longer_needed"
    DAMAGED_OR_DEFECTIVE = "damaged_or_defective"
    WRONG_ITEM = "wrong_item"
    NOT_AS_DESCRIBED = "not_as_described"
    OTHER = "other"


class ReturnStatus(StrEnum):
    LABEL_ISSUED = "label_issued"  # open: waiting for the customer to ship it
    IN_TRANSIT = "in_transit"  # open: on its way back
    RECEIVED = "received"  # open: at the warehouse, refund not issued yet
    COMPLETED = "completed"  # refunded
    CANCELLED = "cancelled"


OPEN_RETURN_STATUSES = frozenset(
    {ReturnStatus.LABEL_ISSUED, ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED}
)


class RefundReason(StrEnum):
    DAMAGED_OR_DEFECTIVE = "damaged_or_defective"
    LOST_IN_TRANSIT = "lost_in_transit"
    LATE_DELIVERY = "late_delivery"
    RETURN_RECEIVED = "return_received"  # issued by the warehouse when a return arrives


class RefundStatus(StrEnum):
    ISSUED = "issued"


class RefundChannel(StrEnum):
    """Who asked for the refund. The velocity rule (REF-6) counts only agent-channel refunds."""

    AGENT = "agent"
    STAFF = "staff"
    WAREHOUSE = "warehouse"


class RefundLineKind(StrEnum):
    ITEM = "item"
    SHIPPING = "shipping"


class ActorType(StrEnum):
    AGENT = "agent"  # a tool call made on behalf of a signed-in customer
    OPERATOR = "operator"
    SYSTEM = "system"  # scheduled jobs: approval expiry, seeding


class WriteAction(StrEnum):
    """The actions that need a preview and a confirmation (D1)."""

    CHANGE_ADDRESS = "change_address"
    CREATE_RETURN = "create_return"
    ISSUE_REFUND = "issue_refund"


class PreviewOutcome(StrEnum):
    COMPLETED = "completed"
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EscalationReason(StrEnum):
    CUSTOMER_REQUESTED = "customer_requested"
    POLICY_DISPUTE = "policy_dispute"
    SUSPECTED_FRAUD = "suspected_fraud"
    COMPLEX_ISSUE = "complex_issue"
    UPSET_CUSTOMER = "upset_customer"
    TECHNICAL_PROBLEM = "technical_problem"
    APPROVAL_EXPIRED = "approval_expired"  # opened by the system, never by the agent
    OTHER = "other"


class EscalationPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class EscalationStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


# ---------------------------------------------------------------- conversations (agent runtime)


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ESCALATED = "escalated"  # handed to a human; new messages get a fixed acknowledgement
    CLOSED = "closed"


class TurnTrigger(StrEnum):
    CUSTOMER_MESSAGE = "customer_message"
    APPROVAL_RESOLVED = "approval_resolved"  # an operator decided; the agent tells the customer


class TurnStatus(StrEnum):
    """A turn's state while it runs, then its terminal state (PLAN.md §5.2)."""

    RUNNING = "running"
    COMPLETED = "completed"
    STEP_LIMIT = "step_limit"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REFUSED = "refused"
    FAILED = "failed"
    PAUSED = "paused"  # the daily spend cap was reached, so no model call was made


class MessageKind(StrEnum):
    """One entry of a conversation's append-only transcript."""

    CUSTOMER = "customer"  # what the customer typed
    ASSISTANT = "assistant"  # a model response, stored exactly as the provider returned it
    TOOL_RESULTS = "tool_results"  # the results of one response's tool calls
    SYSTEM_NOTE = "system_note"  # trusted facts from the runtime: date, approvals, flags
    EVENT = "event"  # the automated user line that lets the agent speak after an approval
    NOTICE = "notice"  # shown to the customer, never sent to the model (handoffs, apologies)


class InboxKind(StrEnum):
    CUSTOMER_MESSAGE = "customer_message"
    APPROVAL_RESOLVED = "approval_resolved"


class ToolCallOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"  # the tool answered with is_error (bad arguments, unknown order, ...)
    DUPLICATE = "duplicate"  # same call earlier in the turn: answered from that result
    REJECTED = "rejected"  # not run: unknown tool, unreadable arguments, too many calls
    UNAVAILABLE = "unavailable"  # timed out or failed in transport, after one retry


class DraftStatus(StrEnum):
    """An eval scenario drafted from a conversation (§10)."""

    GENERATING = "generating"
    VALID = "valid"  # parses, passes scenario validation and contains no real customer details
    INVALID = "invalid"  # the model answered, but the draft fails a check (see its problems)
    FAILED = "failed"  # no draft: the model call failed
