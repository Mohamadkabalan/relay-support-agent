"""The versioned system prompt (PLAN.md §5.5).

Each version is a Markdown template in this package. Its policy section is filled from the same
`policy.yaml` the policy engine enforces, so the prompt can't drift from the rules. A conversation
stores the version and the hash of the rendered text; changing either only affects new
conversations.

The template ends with a canary: a reference string that has no reason to appear in a reply. The
output guard retracts any reply that contains it (a sign the prompt is leaking).
"""

import hashlib
from dataclasses import dataclass
from importlib import resources

from relay.domain.money import format_cents
from relay.domain.policy_config import PolicyConfig

CURRENT_VERSION = "v1"

NON_RETURNABLE_LABELS = {
    "final_sale": "final-sale items",
    "gift_card": "gift cards",
    "digital": "digital products",
}


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    version: str
    text: str
    hash: str
    canary: str


def template(version: str) -> str:
    return resources.files("relay.agent.prompts").joinpath(f"{version}.md").read_text("utf-8")


def canary(version: str) -> str:
    """The version's canary: fixed by the template text, so it never changes for a version."""
    return f"RLY-{hashlib.sha256(template(version).encode()).hexdigest()[:12]}"


def system_prompt(
    policy: PolicyConfig, store_name: str, version: str = CURRENT_VERSION
) -> SystemPrompt:
    source = template(version)
    labels = [NON_RETURNABLE_LABELS[name] for name in policy.returns.non_returnable]
    text = source.format(
        store=store_name,
        return_days=policy.returns.window_days,
        refurbished_days=policy.returns.window_days_refurbished,
        non_returnable=_sentence_list(labels).capitalize(),
        lost_days=policy.refunds.lost_package_days_without_scan,
        late_days=policy.refunds.late_delivery_grace_days,
        settles_in=policy.refunds.settles_in,
        auto_approve=format_cents(policy.refunds.auto_approve_limit_cents),
        velocity_count=policy.refunds.velocity_refunds_before_review,
        velocity_days=policy.refunds.velocity_window_days,
        approval_decision=policy.approvals.expected_decision,
        token_minutes=policy.confirmation.token_ttl_minutes,
        escalation_response=policy.escalation.response_time_text,
        canary=canary(version),
    ).strip()
    return SystemPrompt(
        version=version,
        text=text,
        hash=hashlib.sha256(text.encode()).hexdigest(),
        canary=canary(version),
    )


def _sentence_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"
