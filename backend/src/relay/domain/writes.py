"""The protocol every write follows: preview first, commit in a later turn (PLAN.md §5.9).

Each write action (refund, return, address change) supplies an `ActionHandler`: how to read its
arguments, how to plan it from fresh facts (a policy decision plus a one-line summary), and how to
execute it. `preview_or_commit` runs the protocol around those three pieces, so the rules about
tokens, re-checks, approvals and idempotent replays live in one place.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel

from relay.db.models import ActionPreview, Approval, Order
from relay.domain import audit, previews
from relay.domain.enums import ApprovalStatus, PreviewOutcome, WriteAction
from relay.domain.errors import ConflictError
from relay.domain.facts import facts_for, load_owned_order, lock_customer
from relay.domain.numbers import next_approval_number
from relay.domain.policy import Decision, Facts, Outcome
from relay.domain.policy_config import PolicyConfig
from relay.domain.results import (
    CompletedResult,
    DeniedResult,
    PendingApprovalResult,
    WriteOutcome,
)
from relay.domain.scope import CustomerScope, Deps, Scope

tracer = trace.get_tracer("relay.domain")


class ActionArgs(BaseModel):
    """Arguments of a write. `canonical()` is what the confirmation token is bound to."""

    order_number: str

    def canonical(self) -> dict[str, Any]:
        raise NotImplementedError

    def audit_reason(self) -> str | None:
        return None


@dataclass(frozen=True, slots=True)
class Plan:
    decision: Decision
    summary: str


@dataclass(frozen=True, slots=True)
class ActionHandler[A: ActionArgs, C: CompletedResult]:
    action: WriteAction
    args_model: type[A]
    plan: Callable[[A, Order, Facts, PolicyConfig, datetime], Plan]
    execute: Callable[
        [Deps, Scope, A, Order, Facts, Plan, ActionPreview, Approval | None], Awaitable[C]
    ]
    completed_model: type[C]

    def replay(self, stored: dict[str, Any]) -> WriteOutcome[C]:
        """Rebuild the outcome stored on a committed preview."""
        match stored.get("status"):
            case "denied":
                return DeniedResult.model_validate(stored)
            case "pending_approval":
                return PendingApprovalResult.model_validate(stored)
            case _:
                return self.completed_model.model_validate(stored)


async def preview_or_commit[A: ActionArgs, C: CompletedResult](
    deps: Deps,
    scope: CustomerScope,
    handler: ActionHandler[A, C],
    args: A,
    confirmation_token: str | None,
) -> WriteOutcome[C]:
    session = deps.session
    committing = confirmation_token is not None
    if committing:
        await lock_customer(session, scope.customer_id)
    order = await load_owned_order(session, scope.customer_id, args.order_number, lock=committing)
    canonical = args.canonical()

    preview: ActionPreview | None = None
    if confirmation_token is not None:
        preview = await previews.redeem(
            deps,
            scope,
            action=handler.action,
            token=confirmation_token,
            canonical_args=canonical,
        )
        if preview.committed_at is not None and preview.result is not None:
            return handler.replay(preview.result)

    # Always decide on fresh facts: the order may have changed since the preview.
    now = deps.clock.now()
    facts = await facts_for(session, order, deps.policy, now)
    plan = traced_plan(handler, args, order, facts, deps.policy, now)
    decision = plan.decision

    if decision.outcome is Outcome.DENY:
        denied = denied_result(decision)
        if preview is not None:
            previews.mark_committed(
                preview, PreviewOutcome.DENIED, denied.model_dump(mode="json"), now
            )
        return denied

    if preview is None:
        return await previews.issue(
            deps,
            scope,
            action=handler.action,
            order=order,
            canonical_args=canonical,
            decision=decision,
            summary=plan.summary,
        )

    if preview.amount_cents is not None and preview.amount_cents != decision.amount_cents:
        raise ConflictError(
            "The order changed since the preview, so the amount is different now.",
            "Call the tool again without confirmation_token and show the customer the new preview.",
        )

    result: PendingApprovalResult | C
    if decision.outcome is Outcome.NEEDS_APPROVAL:
        result = await request_approval(deps, scope, args, order, preview, plan)
        outcome = PreviewOutcome.PENDING_APPROVAL
    else:
        result = await handler.execute(deps, scope, args, order, facts, plan, preview, None)
        outcome = PreviewOutcome.COMPLETED
    previews.mark_committed(preview, outcome, result.model_dump(mode="json"), now)
    await session.flush()
    return result


def traced_plan[A: ActionArgs, C: CompletedResult](
    handler: ActionHandler[A, C],
    args: A,
    order: Order,
    facts: Facts,
    policy: PolicyConfig,
    now: datetime,
) -> Plan:
    """`handler.plan` inside a `relay.policy.evaluate` span with the matched rule IDs (§10)."""
    with tracer.start_as_current_span(
        "relay.policy.evaluate", attributes={"relay.action": handler.action.value}
    ) as span:
        plan = handler.plan(args, order, facts, policy, now)
        span.set_attributes(
            {
                "relay.policy.outcome": plan.decision.outcome.value,
                "relay.policy.rules": list(plan.decision.rules),
                "relay.policy.version": plan.decision.policy_version,
            }
        )
    return plan


def denied_result(decision: Decision) -> DeniedResult:
    first = decision.denials[0]
    return DeniedResult(
        rule=first.rule,
        code=first.code,
        explanation=decision.explanation,
        alternatives=list(decision.alternatives),
    )


async def request_approval(
    deps: Deps,
    scope: CustomerScope,
    args: ActionArgs,
    order: Order,
    preview: ActionPreview,
    plan: Plan,
) -> PendingApprovalResult:
    now = deps.clock.now()
    decision = plan.decision
    approval = Approval(
        approval_number=await next_approval_number(deps.session),
        preview_id=preview.id,
        customer_id=scope.customer_id,
        order_id=order.id,
        conversation_id=scope.conversation_id,
        action=preview.action,
        payload=preview.canonical_args,
        summary=plan.summary,
        amount_cents=preview.amount_cents,
        rules=[hit.rule for hit in decision.approval_triggers],
        reason=decision.explanation,
        status=ApprovalStatus.PENDING,
        policy_version=decision.policy_version,
        requested_at=now,
        expires_at=now + timedelta(hours=deps.policy.approvals.expire_after_hours),
    )
    deps.session.add(approval)
    await deps.session.flush()  # assigns approval.id for the audit row
    audit.record(
        deps,
        scope,
        action="approval.request",
        entity="approval",
        entity_id=approval.approval_number,
        on_behalf_of=scope.customer_id,
        after={
            "action": approval.action.value,
            "order_number": order.number,
            "payload": approval.payload,
            "amount_cents": approval.amount_cents,
            "rules": approval.rules,
        },
        reason=args.audit_reason(),
        approval_id=approval.id,
        policy_version=decision.policy_version,
    )
    return PendingApprovalResult(
        approval_number=approval.approval_number,
        summary=plan.summary,
        expected_decision=deps.policy.approvals.expected_decision,
    )


def line_summary(order: Order, quantities: dict[int, int]) -> str:
    """ "1 x Aurora ANC 700 Headphones, 2 x USB-C Cable" for the given line quantities."""
    names = {line.line_no: line.name for line in order.lines}
    return ", ".join(f"{qty} x {names[line_no]}" for line_no, qty in sorted(quantities.items()))


def payment_destination(order: Order) -> str:
    return f"{order.payment_brand} ending {order.payment_last4}"
