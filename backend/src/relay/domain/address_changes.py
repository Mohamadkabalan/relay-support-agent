"""Shipping-address changes (ADR-1 to ADR-4).

Before shipping, the change is immediate. Once a label exists or the package is moving, an operator
has to ask the carrier to reroute it, so the change waits for approval. After that, it's too late.
"""

from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from relay.db.models import ActionPreview, Approval, Order
from relay.domain import audit
from relay.domain.address import Address
from relay.domain.enums import WriteAction
from relay.domain.policy import AddressChangeRequest, Facts, evaluate_address_change
from relay.domain.policy_config import PolicyConfig
from relay.domain.results import AddressChangeCompleted, WriteOutcome
from relay.domain.scope import CustomerScope, Deps, Scope
from relay.domain.writes import ActionArgs, ActionHandler, Plan, preview_or_commit


class AddressChangeArgs(ActionArgs):
    model_config = ConfigDict(frozen=True, extra="forbid")

    address: Address

    def canonical(self) -> dict[str, Any]:
        return {
            "action": WriteAction.CHANGE_ADDRESS.value,
            "order_number": self.order_number.strip().upper(),
            "address": self.address.normalized().model_dump(mode="json"),
        }


def plan(
    args: AddressChangeArgs, order: Order, facts: Facts, policy: PolicyConfig, now: datetime
) -> Plan:
    decision = evaluate_address_change(
        AddressChangeRequest(country=args.address.country), facts.order, policy
    )
    summary = (
        f"Change the shipping address of order {order.number} to "
        f"{args.address.normalized().one_line()}."
    )
    return Plan(decision=decision, summary=summary)


async def execute(
    deps: Deps,
    scope: Scope,
    args: AddressChangeArgs,
    order: Order,
    facts: Facts,
    plan: Plan,
    preview: ActionPreview,
    approval: Approval | None,
) -> AddressChangeCompleted:
    before = dict(order.shipping_address)
    new_address = args.address.normalized()
    order.shipping_address = new_address.model_dump(mode="json")
    await deps.session.flush()
    audit.record(
        deps,
        scope,
        action="order.change_address",
        entity="order",
        entity_id=order.number,
        on_behalf_of=order.customer_id,
        before={"shipping_address": before},
        after={"shipping_address": order.shipping_address},
        reason="customer asked to change the shipping address",
        approval_id=approval.id if approval else None,
        conversation_id=preview.conversation_id,
        policy_version=plan.decision.policy_version,
    )
    return AddressChangeCompleted(order_number=order.number, shipping_address=new_address)


HANDLER = ActionHandler(
    action=WriteAction.CHANGE_ADDRESS,
    args_model=AddressChangeArgs,
    plan=plan,
    execute=execute,
    completed_model=AddressChangeCompleted,
)


async def update_shipping_address(
    deps: Deps,
    scope: CustomerScope,
    args: AddressChangeArgs,
    confirmation_token: str | None = None,
) -> WriteOutcome[AddressChangeCompleted]:
    """The `update_shipping_address` tool: a preview without a token, a commit with one."""
    return await preview_or_commit(deps, scope, HANDLER, args, confirmation_token)
