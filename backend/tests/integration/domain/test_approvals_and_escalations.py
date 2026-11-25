"""Operator decisions, expiry, and hand-offs to a human."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.support.factories import Factory

from relay.clock import FrozenClock
from relay.db.models import Customer, Escalation, Order
from relay.domain import approvals
from relay.domain.address_changes import AddressChangeArgs, update_shipping_address
from relay.domain.enums import (
    ApprovalStatus,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    RefundReason,
)
from relay.domain.errors import ConflictError, InvalidArgumentError, NotFoundError
from relay.domain.escalations import escalate_to_human, resolve_escalation
from relay.domain.refunds import RefundArgs, issue_refund
from relay.domain.results import PendingApprovalResult, PreviewResult
from relay.domain.scope import Deps, SystemScope


async def pending_refund(deps: Deps, factory: Factory, owner: Customer, order: Order) -> str:
    """Preview and confirm a $349.99 damage refund, which needs an operator."""
    args = RefundArgs(
        order_number=order.number, reason=RefundReason.DAMAGED_OR_DEFECTIVE, lines={1: 1}
    )
    first, second = factory.turns(owner)
    shown = await issue_refund(deps, first, args)
    assert isinstance(shown, PreviewResult)
    pending = await issue_refund(deps, second, args, shown.confirmation_token)
    assert isinstance(pending, PendingApprovalResult)
    return pending.approval_number


async def test_approving_a_refund_issues_it_with_operator_authority(
    deps: Deps, factory: Factory
) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)])
    number = await pending_refund(deps, factory, me, order)

    queue = await approvals.list_approvals(deps)
    assert number in [item.approval_number for item in queue]

    view = await approvals.resolve(
        deps, factory.operator_scope(operator), number, approve=True, note="Photo checks out."
    )
    assert view.status is ApprovalStatus.EXECUTED
    assert view.outcome is not None
    assert view.outcome.startswith("RF-")
    request, issued = await factory.audit(me)
    assert (request.action, issued.action) == ("approval.request", "refund.issue")
    assert (issued.actor_type.value, issued.actor_id) == ("operator", str(operator.id))
    assert issued.approval_id is not None
    assert issued.after is not None
    assert issued.after["approval_note"] == "Photo checks out."


async def test_rejecting_records_the_note_and_pays_nothing(deps: Deps, factory: Factory) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)])
    number = await pending_refund(deps, factory, me, order)
    view = await approvals.resolve(
        deps, factory.operator_scope(operator), number, approve=False, note="Needs photos."
    )
    assert (view.status, view.note) == (ApprovalStatus.REJECTED, "Needs photos.")
    assert [row.action for row in await factory.audit(me)] == [
        "approval.request",
        "approval.reject",
    ]


async def test_an_approval_cannot_override_a_denial_that_applies_now(
    deps: Deps, factory: Factory, clock: FrozenClock
) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)], delivered_days_ago=30)
    number = await pending_refund(deps, factory, me, order)
    clock.advance(timedelta(hours=10))  # past midnight: day 31, outside the window
    view = await approvals.resolve(deps, factory.operator_scope(operator), number, approve=True)
    assert view.status is ApprovalStatus.FAILED
    assert view.outcome is not None
    assert "Damage has to be reported within 30 days" in view.outcome
    assert (await factory.audit(me))[-1].action == "approval.fail"


async def test_an_address_change_is_applied_when_approved(deps: Deps, factory: Factory) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, "in_transit")
    args = AddressChangeArgs(
        order_number=order.number,
        address=factory.address("Priya Raman").model_copy(
            update={"city": "Seattle", "state": "WA", "postal_code": "98101"}
        ),
    )
    first, second = factory.turns(me)
    shown = await update_shipping_address(deps, first, args)
    assert isinstance(shown, PreviewResult)
    pending = await update_shipping_address(deps, second, args, shown.confirmation_token)
    assert isinstance(pending, PendingApprovalResult)
    view = await approvals.resolve(
        deps, factory.operator_scope(operator), pending.approval_number, approve=True
    )
    assert view.status is ApprovalStatus.EXECUTED
    stored = await deps.session.get_one(Order, order.id)
    assert stored.shipping_address["city"] == "Seattle"


async def test_a_decision_can_only_be_made_once(deps: Deps, factory: Factory) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)])
    number = await pending_refund(deps, factory, me, order)
    scope = factory.operator_scope(operator)
    await approvals.resolve(deps, scope, number, approve=False)
    with pytest.raises(ConflictError, match="already rejected"):
        await approvals.resolve(deps, scope, number, approve=True)
    with pytest.raises(NotFoundError):
        await approvals.resolve(deps, scope, "APR-99999", approve=True)


async def test_unanswered_approvals_expire_and_open_an_escalation(
    deps: Deps, factory: Factory, clock: FrozenClock
) -> None:
    me, operator = await factory.customer(), await factory.operator()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)])
    number = await pending_refund(deps, factory, me, order)

    clock.advance(timedelta(hours=23, minutes=59))
    assert number not in await approvals.expire_due(deps, SystemScope("approval-expiry"))
    clock.advance(timedelta(minutes=1))
    with pytest.raises(ConflictError, match="expired"):
        await approvals.resolve(deps, factory.operator_scope(operator), number, approve=True)
    assert number in await approvals.expire_due(deps, SystemScope("approval-expiry"))

    view = await approvals.get_approval(deps, number)
    assert view.status is ApprovalStatus.EXPIRED
    escalation = await deps.session.scalar(
        select(Escalation).where(Escalation.customer_id == me.id)
    )
    assert escalation is not None
    assert (escalation.reason, escalation.priority) == (
        EscalationReason.APPROVAL_EXPIRED,
        EscalationPriority.HIGH,
    )
    actions = [row.action for row in await factory.audit(me)]
    assert actions == ["approval.request", "approval.expire", "escalation.open"]


async def test_escalation_is_idempotent_per_conversation(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    scope = factory.scope(me)
    first = await escalate_to_human(
        deps, scope, reason=EscalationReason.CUSTOMER_REQUESTED, summary="Wants a person."
    )
    assert (first.already_open, first.status) == (False, EscalationStatus.OPEN)
    assert first.expected_response == "within 1 business day"
    second = await escalate_to_human(
        deps, scope, reason=EscalationReason.UPSET_CUSTOMER, summary="Still upset."
    )
    assert (second.already_open, second.escalation_number) == (True, first.escalation_number)
    assert [row.action for row in await factory.audit(me)] == ["escalation.open"]


@pytest.mark.parametrize(
    ("reason", "summary"),
    [
        (EscalationReason.APPROVAL_EXPIRED, "x"),
        (EscalationReason.OTHER, "  "),
        (EscalationReason.OTHER, "x" * 1001),
    ],
)
async def test_escalation_input_is_checked(
    deps: Deps, factory: Factory, reason: EscalationReason, summary: str
) -> None:
    me = await factory.customer()
    with pytest.raises(InvalidArgumentError):
        await escalate_to_human(deps, factory.scope(me), reason=reason, summary=summary)


async def test_operators_resolve_escalations(deps: Deps, factory: Factory) -> None:
    me, operator = await factory.customer(), await factory.operator()
    opened = await escalate_to_human(
        deps, factory.scope(me), reason=EscalationReason.OTHER, summary="Question about VoltCare."
    )
    scope = factory.operator_scope(operator)
    resolved = await resolve_escalation(deps, scope, opened.escalation_number, note="Answered.")
    assert resolved.status is EscalationStatus.RESOLVED
    with pytest.raises(ConflictError):
        await resolve_escalation(deps, scope, opened.escalation_number)
    with pytest.raises(NotFoundError):
        await resolve_escalation(deps, scope, "ESC-99999")
