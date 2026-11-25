"""Hand-offs to a human. Always allowed, never previewed: escalating costs nothing to undo.

A conversation has at most one open escalation (a partial unique index enforces it). Asking again
returns the open one instead of creating another.
"""

import uuid

from sqlalchemy import select

from relay.db.models import Escalation
from relay.domain import audit
from relay.domain.enums import EscalationPriority, EscalationReason, EscalationStatus
from relay.domain.errors import ConflictError, InvalidArgumentError, NotFoundError
from relay.domain.numbers import next_escalation_number
from relay.domain.results import EscalationResult
from relay.domain.scope import CustomerScope, Deps, OperatorScope, Scope

MAX_SUMMARY_CHARS = 1000


async def escalate_to_human(
    deps: Deps,
    scope: CustomerScope,
    *,
    reason: EscalationReason,
    summary: str,
    priority: EscalationPriority = EscalationPriority.NORMAL,
) -> EscalationResult:
    """The `escalate_to_human` tool."""
    if reason is EscalationReason.APPROVAL_EXPIRED:
        raise InvalidArgumentError(
            "approval_expired is set by the system, not by the agent.",
            "Pick the reason that matches what the customer needs.",
        )
    return await open_escalation(
        deps,
        scope,
        customer_id=scope.customer_id,
        conversation_id=scope.conversation_id,
        reason=reason,
        summary=summary,
        priority=priority,
    )


async def open_escalation(
    deps: Deps,
    scope: Scope,
    *,
    customer_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    reason: EscalationReason,
    summary: str,
    priority: EscalationPriority,
    approval_id: uuid.UUID | None = None,
) -> EscalationResult:
    summary = summary.strip()
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        raise InvalidArgumentError(
            f"The summary must be 1 to {MAX_SUMMARY_CHARS} characters.",
            "Describe the customer's issue in a few sentences.",
        )
    expected = deps.policy.escalation.response_time_text
    if conversation_id is not None:
        existing = await deps.session.scalar(
            select(Escalation).where(
                Escalation.conversation_id == conversation_id,
                Escalation.status == EscalationStatus.OPEN,
            )
        )
        if existing is not None:
            return _result(existing, expected, already_open=True)

    escalation = Escalation(
        escalation_number=await next_escalation_number(deps.session),
        customer_id=customer_id,
        conversation_id=conversation_id,
        approval_id=approval_id,
        reason=reason,
        summary=summary,
        priority=priority,
        status=EscalationStatus.OPEN,
        created_at=deps.clock.now(),
    )
    deps.session.add(escalation)
    await deps.session.flush()
    audit.record(
        deps,
        scope,
        action="escalation.open",
        entity="escalation",
        entity_id=escalation.escalation_number,
        on_behalf_of=customer_id,
        after={"reason": reason.value, "priority": priority.value, "summary": summary},
        reason=reason.value,
        approval_id=approval_id,
        conversation_id=conversation_id,
    )
    return _result(escalation, expected, already_open=False)


async def resolve_escalation(
    deps: Deps, scope: OperatorScope, escalation_number: str, note: str | None = None
) -> EscalationResult:
    escalation = await deps.session.scalar(
        select(Escalation)
        .where(Escalation.escalation_number == escalation_number.strip().upper())
        .with_for_update()
    )
    if escalation is None:
        raise NotFoundError(f"No escalation {escalation_number}.")
    if escalation.status is not EscalationStatus.OPEN:
        raise ConflictError(f"{escalation.escalation_number} is already resolved.")
    escalation.status = EscalationStatus.RESOLVED
    escalation.resolved_at = deps.clock.now()
    escalation.resolved_by = scope.operator_id
    await deps.session.flush()
    audit.record(
        deps,
        scope,
        action="escalation.resolve",
        entity="escalation",
        entity_id=escalation.escalation_number,
        on_behalf_of=escalation.customer_id,
        before={"status": EscalationStatus.OPEN.value},
        after={"status": EscalationStatus.RESOLVED.value},
        reason=note,
        conversation_id=escalation.conversation_id,
    )
    return _result(escalation, deps.policy.escalation.response_time_text, already_open=False)


def _result(escalation: Escalation, expected: str, *, already_open: bool) -> EscalationResult:
    return EscalationResult(
        escalation_number=escalation.escalation_number,
        status=escalation.status,
        priority=escalation.priority,
        expected_response=expected,
        already_open=already_open,
    )
