"""The audit trail: who did what, for whom, and why.

Every service-level write adds exactly one row, in the same transaction as the write itself, so
the trail can't disagree with the data. Rows can't be changed afterwards: a trigger rejects UPDATE
and DELETE on the table.
"""

import uuid
from typing import Any

from relay.db.models import AuditLog
from relay.domain.scope import CustomerScope, Deps, Scope


def record(
    deps: Deps,
    scope: Scope,
    *,
    action: str,
    entity: str,
    entity_id: str,
    on_behalf_of: uuid.UUID | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    approval_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    policy_version: str | None = None,
) -> AuditLog:
    if conversation_id is None and isinstance(scope, CustomerScope):
        conversation_id = scope.conversation_id
    row = AuditLog(
        occurred_at=deps.clock.now(),
        actor_type=scope.actor_type,
        actor_id=scope.actor_id,
        on_behalf_of=on_behalf_of,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
        approval_id=approval_id,
        conversation_id=conversation_id,
        trace_id=scope.trace_id,
        policy_version=policy_version,
    )
    deps.session.add(row)
    return row
