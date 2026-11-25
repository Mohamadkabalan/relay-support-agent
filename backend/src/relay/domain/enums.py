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
