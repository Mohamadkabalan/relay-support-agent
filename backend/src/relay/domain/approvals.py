"""Operator decisions on writes that need approval (§5.9, rule APR-1).

`resolve` runs in the caller's transaction:

1. Lock the approval row.
2. Re-check the policy on fresh facts, with operator authority. An approval satisfies only
   "needs approval" outcomes; a denial that applies now still wins, and the approval fails.
3. Execute the action and write its audit row.

Pending approvals expire after `approvals.expire_after_hours`; `expire_due` marks them and opens
an escalation so a human follows up.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select

from relay.db.models import ActionPreview, Approval, Order
from relay.domain import address_changes, audit, refunds, returns
from relay.domain.enums import (
    ApprovalStatus,
    EscalationPriority,
    EscalationReason,
    WriteAction,
)
from relay.domain.errors import ConflictError, NotFoundError
from relay.domain.escalations import open_escalation
from relay.domain.facts import facts_for, load_owned_order, lock_customer
from relay.domain.policy import Outcome
from relay.domain.results import ApprovalView
from relay.domain.scope import Deps, OperatorScope, SystemScope
from relay.domain.writes import ActionArgs, ActionHandler

HANDLERS: dict[WriteAction, ActionHandler[Any]] = {
    WriteAction.ISSUE_REFUND: refunds.HANDLER,
    WriteAction.CREATE_RETURN: returns.HANDLER,
    WriteAction.CHANGE_ADDRESS: address_changes.HANDLER,
}


async def resolve(
    deps: Deps,
    scope: OperatorScope,
    approval_number: str,
    *,
    approve: bool,
    note: str | None = None,
) -> ApprovalView:
    session = deps.session
    approval = await _lock(deps, approval_number)
    if approval.status is not ApprovalStatus.PENDING:
        raise ConflictError(f"{approval.approval_number} is already {approval.status.value}.")
    now = deps.clock.now()
    if now >= approval.expires_at:
        raise ConflictError(
            f"{approval.approval_number} expired at {approval.expires_at.isoformat()}."
        )

    note = note.strip() if note else None

    if not approve:
        _decide(approval, ApprovalStatus.REJECTED, scope, note, now)
        await session.flush()
        audit.record(
            deps,
            scope,
            action="approval.reject",
            entity="approval",
            entity_id=approval.approval_number,
            on_behalf_of=approval.customer_id,
            before={"status": ApprovalStatus.PENDING.value},
            after={"status": approval.status.value},
            reason=approval.note,
            approval_id=approval.id,
            conversation_id=approval.conversation_id,
        )
        return await view(deps, approval)

    handler = HANDLERS[approval.action]
    args = _args_from_payload(handler, approval.payload)
    await lock_customer(session, approval.customer_id)
    order = await load_owned_order(session, approval.customer_id, args.order_number, lock=True)
    facts = await facts_for(session, order, deps.policy, now)
    plan = handler.plan(args, order, facts, deps.policy, now)
    decision = plan.decision

    failure: dict[str, Any] | None = None
    if decision.outcome is Outcome.DENY:
        failure = {
            "code": decision.code,
            "rule": decision.rule,
            "explanation": decision.explanation,
        }
    elif approval.amount_cents is not None and decision.amount_cents != approval.amount_cents:
        failure = {
            "code": "AMOUNT_CHANGED",
            "rule": "APR-1",
            "explanation": (
                "The order changed after the request, so the amount is different now. "
                "The customer needs to ask again."
            ),
        }

    if failure is not None:
        _decide(approval, ApprovalStatus.FAILED, scope, note, now)
        approval.execution_result = failure
        await session.flush()
        audit.record(
            deps,
            scope,
            action="approval.fail",
            entity="approval",
            entity_id=approval.approval_number,
            on_behalf_of=approval.customer_id,
            before={"status": ApprovalStatus.PENDING.value},
            after={"status": approval.status.value, **failure},
            reason=approval.note,
            approval_id=approval.id,
            conversation_id=approval.conversation_id,
            policy_version=decision.policy_version,
        )
        return await view(deps, approval)

    preview = await session.get_one(ActionPreview, approval.preview_id)
    _decide(approval, ApprovalStatus.EXECUTED, scope, note, now)
    completed = await handler.execute(deps, scope, args, order, facts, plan, preview, approval)
    approval.execution_result = completed.model_dump(mode="json")
    await session.flush()
    return await view(deps, approval)


async def expire_due(deps: Deps, scope: SystemScope) -> list[str]:
    """Expire every pending approval past its deadline. Returns their numbers."""
    now = deps.clock.now()
    due = await deps.session.scalars(
        select(Approval)
        .where(Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= now)
        .order_by(Approval.requested_at)
        .with_for_update(skip_locked=True)
    )
    expired: list[str] = []
    for approval in due.all():
        approval.status = ApprovalStatus.EXPIRED
        approval.decided_at = now
        await deps.session.flush()
        audit.record(
            deps,
            scope,
            action="approval.expire",
            entity="approval",
            entity_id=approval.approval_number,
            on_behalf_of=approval.customer_id,
            before={"status": ApprovalStatus.PENDING.value},
            after={"status": approval.status.value},
            reason=f"no decision within {deps.policy.approvals.expire_after_hours} hours",
            approval_id=approval.id,
            conversation_id=approval.conversation_id,
        )
        await open_escalation(
            deps,
            scope,
            customer_id=approval.customer_id,
            conversation_id=approval.conversation_id,
            reason=EscalationReason.APPROVAL_EXPIRED,
            summary=f"{approval.approval_number} expired without a decision: {approval.summary}",
            priority=EscalationPriority.HIGH,
            approval_id=approval.id,
        )
        expired.append(approval.approval_number)
    return expired


async def list_approvals(
    deps: Deps, status: ApprovalStatus | None = ApprovalStatus.PENDING
) -> list[ApprovalView]:
    query = select(Approval).order_by(Approval.requested_at)
    if status is not None:
        query = query.where(Approval.status == status)
    return [await view(deps, approval) for approval in await deps.session.scalars(query)]


async def get_approval(deps: Deps, approval_number: str) -> ApprovalView:
    approval = await deps.session.scalar(
        select(Approval).where(Approval.approval_number == approval_number.strip().upper())
    )
    if approval is None:
        raise NotFoundError(f"No approval {approval_number}.")
    return await view(deps, approval)


async def view(deps: Deps, approval: Approval) -> ApprovalView:
    order_number = await deps.session.scalar(
        select(Order.number).where(Order.id == approval.order_id)
    )
    outcome = None
    if approval.execution_result is not None:
        outcome = str(
            approval.execution_result.get("explanation")
            or approval.execution_result.get("refund_number")
            or approval.execution_result.get("rma_number")
            or approval.execution_result.get("status")
        )
    return ApprovalView(
        approval_number=approval.approval_number,
        status=approval.status,
        action=approval.action,
        order_number=order_number or "",
        summary=approval.summary,
        amount_cents=approval.amount_cents,
        rules=list(approval.rules),
        reason=approval.reason,
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        note=approval.note,
        outcome=outcome,
    )


def _decide(
    approval: Approval,
    status: ApprovalStatus,
    scope: OperatorScope,
    note: str | None,
    now: datetime,
) -> None:
    """Set the outcome and who decided it together: a CHECK constraint ties them."""
    approval.status = status
    approval.decided_at = now
    approval.decided_by = scope.operator_id
    approval.note = note


async def _lock(deps: Deps, approval_number: str) -> Approval:
    approval = await deps.session.scalar(
        select(Approval)
        .where(Approval.approval_number == approval_number.strip().upper())
        .with_for_update()
    )
    if approval is None:
        raise NotFoundError(f"No approval {approval_number}.")
    return approval


def _args_from_payload(handler: ActionHandler[Any], payload: dict[str, Any]) -> ActionArgs:
    fields = {key: value for key, value in payload.items() if key != "action"}
    args: ActionArgs = handler.args_model.model_validate(fields)
    return args
