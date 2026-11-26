"""Returns: an RMA and a prepaid label for delivered items inside the window (RET-1 to RET-4).

Returns never need an operator. The refund is issued by the warehouse when the items arrive, so
creating a return moves no money; the result carries an estimate.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import ConfigDict, Field, StringConstraints

from relay.db.models import ActionPreview, Approval, Order, Return, ReturnLine
from relay.domain import audit
from relay.domain.enums import ReturnReason, ReturnStatus, WriteAction
from relay.domain.money import format_cents
from relay.domain.numbers import next_rma_number
from relay.domain.policy import Facts, LineRequest, ReturnRequest, evaluate_return
from relay.domain.policy_config import PolicyConfig
from relay.domain.results import ReturnCompleted, WriteOutcome
from relay.domain.scope import CustomerScope, Deps, Scope
from relay.domain.writes import (
    ActionArgs,
    ActionHandler,
    Plan,
    line_summary,
    payment_destination,
    preview_or_commit,
)

LABEL_URL = "https://returns.pixelvolt.example/labels/{rma}.pdf"
INSTRUCTIONS = (
    "Print the label, pack the items with their accessories, and drop the parcel off with the "
    "carrier. The refund goes to the original payment method once the return arrives."
)

Details = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class ReturnArgs(ActionArgs):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: dict[int, Annotated[int, Field(ge=1, le=100)]]
    reason: ReturnReason
    details: Details | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "action": WriteAction.CREATE_RETURN.value,
            "order_number": self.order_number.strip().upper(),
            "reason": self.reason.value,
            "lines": {str(line): qty for line, qty in sorted(self.lines.items())},
        }

    def audit_reason(self) -> str | None:
        details = f": {self.details}" if self.details else ""
        return f"{self.reason.value}{details}"

    def to_request(self) -> ReturnRequest:
        return ReturnRequest(
            lines=tuple(LineRequest(line, qty) for line, qty in sorted(self.lines.items())),
            reason=self.reason,
        )


def plan(args: ReturnArgs, order: Order, facts: Facts, policy: PolicyConfig, now: datetime) -> Plan:
    decision = evaluate_return(args.to_request(), facts.order, policy, now)
    summary = (
        f"Return {line_summary(order, args.lines)} from order {order.number} "
        f"({args.reason.value.replace('_', ' ')}). We'll email a prepaid label, and about "
        f"{format_cents(decision.amount_cents)} goes back to the {payment_destination(order)} "
        "once the items arrive."
    )
    return Plan(decision=decision, summary=summary)


async def execute(
    deps: Deps,
    scope: Scope,
    args: ReturnArgs,
    order: Order,
    facts: Facts,
    plan: Plan,
    preview: ActionPreview,
    approval: Approval | None,
) -> ReturnCompleted:
    rma = await next_rma_number(deps.session)
    line_ids = {line.line_no: line.id for line in order.lines}
    return_ = Return(
        rma_number=rma,
        order_id=order.id,
        status=ReturnStatus.LABEL_ISSUED,
        reason=args.reason,
        details=args.details,
        label_url=LABEL_URL.format(rma=rma),
        created_at=deps.clock.now(),
        created_by_type=scope.actor_type,
        preview_id=preview.id,
        conversation_id=preview.conversation_id,
        lines=[
            ReturnLine(order_line_id=line_ids[line_no], quantity=qty)
            for line_no, qty in sorted(args.lines.items())
        ],
    )
    deps.session.add(return_)
    await deps.session.flush()
    audit.record(
        deps,
        scope,
        action="return.create",
        entity="return",
        entity_id=rma,
        on_behalf_of=order.customer_id,
        after={
            "order_number": order.number,
            "reason": args.reason.value,
            "lines": args.canonical()["lines"],
            "refund_estimate_cents": plan.decision.amount_cents,
        },
        reason=args.audit_reason(),
        approval_id=approval.id if approval else None,
        conversation_id=preview.conversation_id,
        policy_version=plan.decision.policy_version,
    )
    return ReturnCompleted(
        rma_number=rma,
        label_url=return_.label_url,
        instructions=INSTRUCTIONS,
        refund_estimate_cents=plan.decision.amount_cents,
    )


HANDLER = ActionHandler(
    action=WriteAction.CREATE_RETURN,
    args_model=ReturnArgs,
    plan=plan,
    execute=execute,
    completed_model=ReturnCompleted,
)


async def create_return(
    deps: Deps, scope: CustomerScope, args: ReturnArgs, confirmation_token: str | None = None
) -> WriteOutcome[ReturnCompleted]:
    """The `create_return` tool: a preview without a token, a commit with one."""
    return await preview_or_commit(deps, scope, HANDLER, args, confirmation_token)
