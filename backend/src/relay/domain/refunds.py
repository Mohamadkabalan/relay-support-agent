"""Refunds: damaged or defective items, lost packages, late deliveries (REF-1 to REF-6).

The amount is always computed here, from the order lines; no caller can name an amount or a
destination. Money goes back to the original payment method.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from relay.db.models import ActionPreview, Approval, Order, Refund, RefundLine
from relay.domain import audit
from relay.domain.enums import (
    RefundChannel,
    RefundLineKind,
    RefundReason,
    RefundStatus,
    WriteAction,
)
from relay.domain.money import format_cents
from relay.domain.numbers import next_refund_number
from relay.domain.policy import Facts, LineRequest, RefundRequest, evaluate_refund
from relay.domain.policy_config import PolicyConfig
from relay.domain.results import RefundCompleted, WriteOutcome
from relay.domain.scope import CustomerScope, Deps, Scope
from relay.domain.writes import (
    ActionArgs,
    ActionHandler,
    Plan,
    line_summary,
    payment_destination,
    preview_or_commit,
)

Report = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]

AGENT_REASONS = (
    RefundReason.DAMAGED_OR_DEFECTIVE,
    RefundReason.LOST_IN_TRANSIT,
    RefundReason.LATE_DELIVERY,
)


class RefundArgs(ActionArgs):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: RefundReason
    lines: dict[int, Annotated[int, Field(ge=1, le=100)]] = Field(default_factory=dict)
    customer_report: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
    ) = None

    @field_validator("reason")
    @classmethod
    def _agent_reasons_only(cls, reason: RefundReason) -> RefundReason:
        if reason not in AGENT_REASONS:
            raise ValueError(f"reason must be one of {', '.join(AGENT_REASONS)}")
        return reason

    def canonical(self) -> dict[str, Any]:
        # The customer's own words aren't part of what they confirm, so rephrasing them between
        # the preview and the commit doesn't invalidate the token.
        return {
            "action": WriteAction.ISSUE_REFUND.value,
            "order_number": self.order_number.strip().upper(),
            "reason": self.reason.value,
            "lines": {str(line): qty for line, qty in sorted(self.lines.items())},
        }

    def audit_reason(self) -> str | None:
        report = f": {self.customer_report}" if self.customer_report else ""
        return f"{self.reason.value}{report}"

    def to_request(self) -> RefundRequest:
        return RefundRequest(
            reason=self.reason,
            lines=tuple(LineRequest(line, qty) for line, qty in sorted(self.lines.items())),
        )


def plan(args: RefundArgs, order: Order, facts: Facts, policy: PolicyConfig, now: datetime) -> Plan:
    decision = evaluate_refund(args.to_request(), facts, policy, now)
    amount = format_cents(decision.amount_cents)
    destination = payment_destination(order)
    match args.reason:
        case RefundReason.DAMAGED_OR_DEFECTIVE:
            summary = (
                f"Refund {amount} to the {destination} for {line_summary(order, args.lines)} "
                f"from order {order.number} (damaged or defective)."
            )
        case RefundReason.LOST_IN_TRANSIT:
            summary = (
                f"Refund {amount} to the {destination} for order {order.number}, which the "
                "carrier lost."
            )
        case _:
            summary = (
                f"Refund the {amount} shipping fee on order {order.number} to the {destination} "
                "(late delivery)."
            )
    return Plan(decision=decision, summary=summary)


async def execute(
    deps: Deps,
    scope: Scope,
    args: RefundArgs,
    order: Order,
    facts: Facts,
    plan: Plan,
    preview: ActionPreview,
    approval: Approval | None,
) -> RefundCompleted:
    lines = _refund_lines(args, order, facts)
    if sum(line.amount_cents for line in lines) != plan.decision.amount_cents:  # pragma: no cover
        raise RuntimeError("Refund lines don't add up to the amount the policy decided")
    refund = Refund(
        refund_number=await next_refund_number(deps.session),
        order_id=order.id,
        reason=args.reason,
        channel=RefundChannel.AGENT,
        amount_cents=plan.decision.amount_cents,
        status=RefundStatus.ISSUED,
        issued_at=deps.clock.now(),
        preview_id=preview.id,
        approval_id=approval.id if approval else None,
        conversation_id=preview.conversation_id,
        lines=lines,
    )
    deps.session.add(refund)
    await deps.session.flush()
    audit.record(
        deps,
        scope,
        action="refund.issue",
        entity="refund",
        entity_id=refund.refund_number,
        on_behalf_of=order.customer_id,
        after={
            "order_number": order.number,
            "reason": args.reason.value,
            "amount_cents": refund.amount_cents,
            "lines": args.canonical()["lines"],
            "destination": payment_destination(order),
            "approval_note": approval.note if approval else None,
        },
        reason=args.audit_reason(),
        approval_id=approval.id if approval else None,
        conversation_id=preview.conversation_id,
        policy_version=plan.decision.policy_version,
    )
    return RefundCompleted(
        refund_number=refund.refund_number,
        amount_cents=refund.amount_cents,
        destination=payment_destination(order),
        settles_in=deps.policy.refunds.settles_in,
    )


def _refund_lines(args: RefundArgs, order: Order, facts: Facts) -> list[RefundLine]:
    by_number = {line.line_no: line for line in order.lines}
    if args.reason is RefundReason.DAMAGED_OR_DEFECTIVE:
        return [
            RefundLine(
                kind=RefundLineKind.ITEM,
                order_line_id=by_number[line_no].id,
                quantity=qty,
                amount_cents=by_number[line_no].unit_price_cents * qty,
            )
            for line_no, qty in sorted(args.lines.items())
        ]
    lines: list[RefundLine] = []
    if args.reason is RefundReason.LOST_IN_TRANSIT:
        for line_facts in facts.order.lines:
            line = by_number[line_facts.line_no]
            if line_facts.available_qty and line.unit_price_cents:
                lines.append(
                    RefundLine(
                        kind=RefundLineKind.ITEM,
                        order_line_id=line.id,
                        quantity=line_facts.available_qty,
                        amount_cents=line.unit_price_cents * line_facts.available_qty,
                    )
                )
    if not facts.order.shipping_refunded and order.shipping_cents:
        lines.append(RefundLine(kind=RefundLineKind.SHIPPING, amount_cents=order.shipping_cents))
    return lines


HANDLER = ActionHandler(
    action=WriteAction.ISSUE_REFUND,
    args_model=RefundArgs,
    plan=plan,
    execute=execute,
    completed_model=RefundCompleted,
)


async def issue_refund(
    deps: Deps, scope: CustomerScope, args: RefundArgs, confirmation_token: str | None = None
) -> WriteOutcome[RefundCompleted]:
    """The `issue_refund` tool: a preview without a token, a commit with one."""
    return await preview_or_commit(deps, scope, HANDLER, args, confirmation_token)
