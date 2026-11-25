"""issue_refund: preview, confirmation across turns, approvals, denials and idempotent commits."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from tests.support.factories import Factory

from relay.clock import FrozenClock
from relay.db.models import ActionPreview, Approval, Refund
from relay.domain.enums import PreviewOutcome, RefundChannel, RefundReason, ReturnReason
from relay.domain.errors import (
    ConfirmationInvalidError,
    ConfirmationTooEarlyError,
    ConflictError,
    OrderNotFoundError,
)
from relay.domain.refunds import RefundArgs, issue_refund
from relay.domain.results import (
    DeniedResult,
    PendingApprovalResult,
    PreviewResult,
    RefundCompleted,
)
from relay.domain.returns import ReturnArgs, create_return
from relay.domain.scope import CustomerScope, Deps
from relay.seed import builders


def damaged(number: str, line: int = 1, qty: int = 1, report: str | None = None) -> RefundArgs:
    return RefundArgs(
        order_number=number,
        reason=RefundReason.DAMAGED_OR_DEFECTIVE,
        lines={line: qty},
        customer_report=report,
    )


async def preview(deps: Deps, scope: CustomerScope, args: RefundArgs) -> PreviewResult:
    result = await issue_refund(deps, scope, args)
    assert isinstance(result, PreviewResult), result
    return result


async def refunds_count(deps: Deps) -> int:
    return await deps.session.scalar(select(func.count()).select_from(Refund)) or 0


async def test_small_damage_refund_completes_on_confirmation(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])  # $49.99
    first, second = factory.turns(me)
    before = await refunds_count(deps)

    shown = await preview(deps, first, damaged(order.number))
    assert shown.amount_cents == 4999
    assert not shown.needs_operator_approval
    assert shown.summary == (
        f"Refund $49.99 to the Visa ending 4242 for 1 x Volt 65 W Charger from order "
        f"{order.number} (damaged or defective)."
    )
    assert await refunds_count(deps) == before  # a preview writes no refund
    assert await factory.audit(me) == []  # ... and no audit row

    done = await issue_refund(
        deps, second, damaged(order.number, report="It won't charge"), shown.confirmation_token
    )
    assert isinstance(done, RefundCompleted)
    assert (done.amount_cents, done.destination) == (4999, "Visa ending 4242")
    refund = await deps.session.scalar(
        select(Refund).where(Refund.refund_number == done.refund_number)
    )
    assert refund is not None
    assert refund.conversation_id == second.conversation_id
    [audit] = await factory.audit(me)
    assert (audit.action, audit.entity_id, audit.actor_type.value) == (
        "refund.issue",
        done.refund_number,
        "agent",
    )
    assert audit.reason == "damaged_or_defective: It won't charge"
    assert audit.conversation_id == second.conversation_id


async def test_refund_over_the_limit_waits_for_an_operator(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("AUD-ANC700", 1)])  # $349.99
    first, second = factory.turns(me)

    shown = await preview(deps, first, damaged(order.number))
    assert shown.needs_operator_approval
    assert shown.approval_reason is not None
    assert "over $100.00" in shown.approval_reason

    pending = await issue_refund(deps, second, damaged(order.number), shown.confirmation_token)
    assert isinstance(pending, PendingApprovalResult)
    approval = await deps.session.scalar(
        select(Approval).where(Approval.approval_number == pending.approval_number)
    )
    assert approval is not None
    assert (approval.amount_cents, approval.rules) == (34999, ["REF-5"])
    assert [row.action for row in await factory.audit(me)] == ["approval.request"]


async def test_confirmation_in_the_same_turn_is_too_early(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, _ = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number))
    with pytest.raises(ConfirmationTooEarlyError):
        await issue_refund(deps, first, damaged(order.number), shown.confirmation_token)


async def test_token_is_bound_to_the_arguments(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 2)])
    first, second = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number, qty=1))
    with pytest.raises(ConfirmationInvalidError, match="arguments differ"):
        await issue_refund(deps, second, damaged(order.number, qty=2), shown.confirmation_token)


async def test_rephrasing_the_customer_report_keeps_the_token_valid(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, second = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number, report="broken"))
    done = await issue_refund(
        deps, second, damaged(order.number, report="It's broken"), shown.confirmation_token
    )
    assert isinstance(done, RefundCompleted)


async def test_token_is_bound_to_the_conversation_and_customer(
    deps: Deps, factory: Factory
) -> None:
    me, other = await factory.customer(), await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, _ = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number))

    elsewhere = factory.scope(me, turn=5)  # same customer, another conversation
    with pytest.raises(ConfirmationInvalidError, match="not valid in this conversation"):
        await issue_refund(deps, elsewhere, damaged(order.number), shown.confirmation_token)
    stranger = factory.scope(other, conversation=first.conversation_id, turn=2)
    with pytest.raises(OrderNotFoundError):  # ownership fails before the token is even read
        await issue_refund(deps, stranger, damaged(order.number), shown.confirmation_token)


@pytest.mark.parametrize("token", ["cfm_made_up", "", "   "])
async def test_unknown_tokens_are_invalid(deps: Deps, factory: Factory, token: str) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    _, second = factory.turns(me)
    with pytest.raises(ConfirmationInvalidError):
        await issue_refund(deps, second, damaged(order.number), token)


async def test_tokens_expire_after_30_minutes(
    deps: Deps, factory: Factory, clock: FrozenClock
) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, second = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number))
    clock.advance(timedelta(minutes=30))
    with pytest.raises(ConfirmationInvalidError, match="expired"):
        await issue_refund(deps, second, damaged(order.number), shown.confirmation_token)


async def test_a_retried_commit_replays_instead_of_paying_twice(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, second = factory.turns(me)
    shown = await preview(deps, first, damaged(order.number))
    before = await refunds_count(deps)

    done = await issue_refund(deps, second, damaged(order.number), shown.confirmation_token)
    again = await issue_refund(deps, second, damaged(order.number), shown.confirmation_token)
    assert again == done
    assert await refunds_count(deps) == before + 1
    assert len(await factory.audit(me)) == 1


async def test_a_denied_request_gets_no_token(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)], delivered_days_ago=45)
    first, _ = factory.turns(me)
    result = await issue_refund(deps, first, damaged(order.number))
    assert isinstance(result, DeniedResult)
    assert (result.rule, result.code) == ("REF-1", "OUTSIDE_WINDOW")
    count = await deps.session.scalar(
        select(func.count()).select_from(ActionPreview).where(ActionPreview.customer_id == me.id)
    )
    assert count == 0


async def test_the_policy_is_checked_again_at_commit(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, second = factory.turns(me)
    a = await preview(deps, first, damaged(order.number))
    b = await preview(deps, first, damaged(order.number))  # the same unit, previewed twice

    assert isinstance(
        await issue_refund(deps, second, damaged(order.number), a.confirmation_token),
        RefundCompleted,
    )
    denied = await issue_refund(deps, second, damaged(order.number), b.confirmation_token)
    assert isinstance(denied, DeniedResult)
    assert denied.code == "ALREADY_REFUNDED"
    stored = await deps.session.scalar(
        select(ActionPreview).where(ActionPreview.token == b.confirmation_token)
    )
    assert stored is not None
    assert stored.outcome is PreviewOutcome.DENIED


async def test_commit_fails_if_the_amount_changed_since_the_preview(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(
        me, "in_transit", lines=[("ACC-CHG65", 1), ("ACC-USBC1M", 1)], last_scan_days_ago=9
    )
    first, second = factory.turns(me)
    lost = RefundArgs(order_number=order.number, reason=RefundReason.LOST_IN_TRANSIT)
    shown = await preview(deps, first, lost)
    assert shown.amount_cents == 4999 + 999

    # Meanwhile a staff member refunds the cable on its own, so the lost-package amount drops
    # and the customer's confirmation no longer matches what would be paid.
    deps.session.add(
        builders.refund(
            factory.ids,
            order,
            refund_number="RF-99999",
            issued_at=factory.now,
            reason=RefundReason.DAMAGED_OR_DEFECTIVE,
            channel=RefundChannel.STAFF,
            lines={2: 1},
        )
    )
    await deps.session.flush()
    with pytest.raises(ConflictError, match="amount is different"):
        await issue_refund(deps, second, lost, shown.confirmation_token)


async def test_velocity_customers_need_approval_even_for_small_refunds(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    for _ in range(2):
        order = await factory.order(me, lines=[("ACC-USBC1M", 1)])
        first, second = factory.turns(me)
        shown = await preview(deps, first, damaged(order.number))
        assert isinstance(
            await issue_refund(deps, second, damaged(order.number), shown.confirmation_token),
            RefundCompleted,
        )
    third = await factory.order(me, lines=[("ACC-USBC1M", 1)])
    first, _ = factory.turns(me)
    shown = await preview(deps, first, damaged(third.number))
    assert shown.needs_operator_approval
    assert shown.approval_reason is not None
    assert "several refunds recently" in shown.approval_reason


async def test_late_delivery_refunds_the_shipping_fee_once(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, late_by=4, shipping_cents=1499)
    first, second = factory.turns(me)
    late = RefundArgs(order_number=order.number, reason=RefundReason.LATE_DELIVERY)
    shown = await preview(deps, first, late)
    done = await issue_refund(deps, second, late, shown.confirmation_token)
    assert isinstance(done, RefundCompleted)
    assert done.amount_cents == 1499
    again = await issue_refund(deps, factory.turns(me)[0], late)
    assert isinstance(again, DeniedResult)
    assert again.code == "ALREADY_REFUNDED"


async def test_over_stdio_there_are_no_turns_to_wait_for(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    desktop = CustomerScope(customer_id=me.id)
    shown = await preview(deps, desktop, damaged(order.number))
    done = await issue_refund(deps, desktop, damaged(order.number), shown.confirmation_token)
    assert isinstance(done, RefundCompleted)
    [audit] = await factory.audit(me)
    assert audit.actor_id == "stdio"


async def test_refund_reasons_are_limited_to_the_agent_ones() -> None:
    with pytest.raises(ValueError, match="reason must be one of"):
        RefundArgs(order_number="PV-100001", reason=RefundReason.RETURN_RECEIVED)


async def test_retries_replay_denials_and_pending_approvals_too(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    big = await factory.order(me, lines=[("AUD-ANC700", 1)])
    first, second = factory.turns(me)
    shown = await preview(deps, first, damaged(big.number))
    pending = await issue_refund(deps, second, damaged(big.number), shown.confirmation_token)
    assert isinstance(pending, PendingApprovalResult)
    replay = await issue_refund(deps, second, damaged(big.number), shown.confirmation_token)
    assert replay == pending

    small = await factory.order(me, lines=[("ACC-CHG65", 1)])
    a = await preview(deps, first, damaged(small.number))
    b = await preview(deps, first, damaged(small.number))
    await issue_refund(deps, second, damaged(small.number), a.confirmation_token)
    denied = await issue_refund(deps, second, damaged(small.number), b.confirmation_token)
    assert isinstance(denied, DeniedResult)
    assert await issue_refund(deps, second, damaged(small.number), b.confirmation_token) == denied


async def test_a_token_only_confirms_the_action_it_was_issued_for(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-CHG65", 1)])
    first, second = factory.turns(me)
    shown = await create_return(
        deps,
        first,
        ReturnArgs(order_number=order.number, lines={1: 1}, reason=ReturnReason.CHANGED_MIND),
    )
    assert isinstance(shown, PreviewResult)
    with pytest.raises(ConfirmationInvalidError, match="is for create_return, not issue_refund"):
        await issue_refund(deps, second, damaged(order.number), shown.confirmation_token)
