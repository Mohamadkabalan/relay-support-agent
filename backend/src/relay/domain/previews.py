"""Confirm-then-commit (D1, ADR-0004, rule CNF-1).

A write tool called without a token gets a preview and a single-use token. Called again with the
token, it commits, but only if the token:

- exists and was issued to this customer, in this conversation,
- names the same action with the same canonical arguments,
- was issued in an earlier turn (the customer has seen the preview and answered),
- has not expired, and has not been used.

A token that was already used replays the stored outcome instead of acting twice, which makes a
retried commit idempotent.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from relay.db.models import ActionPreview, Order
from relay.domain.enums import PreviewOutcome, WriteAction
from relay.domain.errors import ConfirmationInvalidError, ConfirmationTooEarlyError
from relay.domain.policy import Decision, Outcome
from relay.domain.results import PreviewResult
from relay.domain.scope import CustomerScope, Deps

TOKEN_PREFIX = "cfm_"  # noqa: S105 (a prefix, not a secret)


def args_hash(canonical_args: dict[str, Any]) -> str:
    encoded = json.dumps(canonical_args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def issue(
    deps: Deps,
    scope: CustomerScope,
    *,
    action: WriteAction,
    order: Order,
    canonical_args: dict[str, Any],
    decision: Decision,
    summary: str,
) -> PreviewResult:
    now = deps.clock.now()
    needs_approval = decision.outcome is Outcome.NEEDS_APPROVAL
    preview = ActionPreview(
        token=TOKEN_PREFIX + secrets.token_urlsafe(12),
        customer_id=scope.customer_id,
        order_id=order.id,
        conversation_id=scope.conversation_id,
        issued_turn_seq=scope.turn_seq,
        action=action,
        canonical_args=canonical_args,
        args_hash=args_hash(canonical_args),
        summary=summary,
        amount_cents=decision.amount_cents if action is not WriteAction.CHANGE_ADDRESS else None,
        needs_approval=needs_approval,
        policy_version=decision.policy_version,
        created_at=now,
        expires_at=now + timedelta(minutes=deps.policy.confirmation.token_ttl_minutes),
    )
    deps.session.add(preview)
    await deps.session.flush()
    return PreviewResult(
        confirmation_token=preview.token,
        expires_at=preview.expires_at,
        summary=summary,
        amount_cents=preview.amount_cents,
        needs_operator_approval=needs_approval,
        approval_reason=decision.explanation if needs_approval else None,
    )


async def redeem(
    deps: Deps,
    scope: CustomerScope,
    *,
    action: WriteAction,
    token: str,
    canonical_args: dict[str, Any],
) -> ActionPreview:
    """Check a confirmation token and lock its preview. Raises if it can't be used.

    The caller checks `preview.committed_at`: if it is set, the commit already happened and the
    caller replays `preview.result`.
    """
    preview = await deps.session.scalar(
        select(ActionPreview).where(ActionPreview.token == token.strip()).with_for_update()
    )
    # Someone else's token looks exactly like an unknown one.
    if (
        preview is None
        or preview.customer_id != scope.customer_id
        or preview.conversation_id != scope.conversation_id
    ):
        raise ConfirmationInvalidError(
            "This confirmation token is not valid in this conversation.",
            "Call the tool again without confirmation_token to get a new preview.",
        )
    if preview.action is not action:
        raise ConfirmationInvalidError(
            f"This confirmation token is for {preview.action.value}, not {action.value}.",
            "Use the token returned by this tool's own preview.",
        )
    if preview.args_hash != args_hash(canonical_args):
        raise ConfirmationInvalidError(
            "The arguments differ from the ones in the preview.",
            "Call the tool again without confirmation_token, show the new preview to the "
            "customer, and ask them to confirm it.",
        )
    if preview.committed_at is not None:
        return preview  # a retried commit: the caller replays the stored outcome
    if deps.clock.now() >= preview.expires_at:
        raise ConfirmationInvalidError(
            "This confirmation token has expired.",
            "Call the tool again without confirmation_token to get a fresh preview.",
        )
    if _same_turn(preview, scope):
        raise ConfirmationTooEarlyError()
    return preview


def mark_committed(
    preview: ActionPreview, outcome: PreviewOutcome, result: dict[str, Any], now: datetime
) -> None:
    preview.committed_at = now
    preview.outcome = outcome
    preview.result = result


def _same_turn(preview: ActionPreview, scope: CustomerScope) -> bool:
    # Over stdio there are no turns; Claude Desktop's own approval prompt stands in (§5.9).
    if preview.issued_turn_seq is None or scope.turn_seq is None:
        return False
    return scope.turn_seq <= preview.issued_turn_seq
