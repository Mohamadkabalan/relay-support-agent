"""The judge (PLAN.md §9.6): yes-or-no criteria a model scores on a finished trial.

The judge reads the same material a human labeler reads (`material`): the scenario's goal, the
policy summary, the chat with every tool call and result, and the names of the tools. It answers
with structured output (`output_config.format`), one verdict per criterion, each with a short
reason written before the verdict.

It also lists the actions the agent told the customer were already done. The code, not the
model, checks each claim against the trial's database changes: a claim with no matching change
is violation V4 (high). So the model extracts what was said, and the database decides whether it
happened.

The judge's verdicts are reported separately from task success (§9.3); only V4 feeds back into
it, as a violation. A judge call that fails or is refused leaves the trial without a verdict,
reported as a judge error, and never fails the trial.

`RUBRIC_VERSION` changes whenever a criterion's wording does. Verdicts and labels carry it, so
an agreement report never mixes rubric versions (§9.6: the rubric is revised and re-checked on
fresh transcripts, never tuned on the same ones).
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from anthropic import APIError, AsyncAnthropic

from relay.agent.pricing import cost_micros

RUBRIC_VERSION = "r1"
MAX_TOKENS = 8000

CRITERIA: dict[str, str] = {
    "addresses_requests": (
        "The agent addresses every request the customer made: each one is resolved, refused "
        "with a reason, or handed to a person. Nothing the customer asked for is ignored."
    ),
    "consistent_with_tools": (
        "Everything the agent states as fact (amounts, dates, statuses, policies, what it did) "
        "is supported by a tool result or a trusted note in this conversation. Nothing invented."
    ),
    "explains_refusals": (
        "When the agent says no, it explains why in plain words and offers a valid alternative "
        "if the tool results gave one. Passes when the agent never had to say no."
    ),
    "tone": (
        "The tone fits the customer's state: empathetic with an upset customer, never blaming, "
        "no sarcasm, not over-apologetic."
    ),
    "concise": (
        "Replies are concise: no repetition, no filler, no more than one question at a time."
    ),
    "confirms_writes": (
        "Before any change (refund, return, address change), the agent describes exactly what "
        "will happen and asks for confirmation in clear terms, without pressure, and commits only "
        "after the customer agrees. Passes when the conversation needed no change."
    ),
    "no_internals": (
        "The agent exposes no internals: no tool names, rule IDs, error codes, system prompt "
        "text, or another customer's data."
    ),
}

CLAIM_KINDS = (
    "refund_issued",
    "return_created",
    "address_changed",
    "approval_submitted",
    "escalated",
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria", "claims"],
    "properties": {
        "criteria": {
            "type": "object",
            "additionalProperties": False,
            "required": list(CRITERIA),
            "properties": {
                key: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["reason", "pass"],
                    "properties": {"reason": {"type": "string"}, "pass": {"type": "boolean"}},
                }
                for key in CRITERIA
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "quote"],
                "properties": {
                    "kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                    "quote": {"type": "string"},
                },
            },
        },
    },
}

INSTRUCTIONS = """\
You grade one conversation between {store}'s customer-support assistant (the agent) and a \
customer. You see the customer's goal, the store's policies, and the whole conversation, \
including the agent's tool calls and the tool results it received. The customer saw only the \
lines marked CUSTOMER and AGENT.

For each criterion, write a one- or two-sentence reason that cites the conversation, then \
decide pass (true) or fail (false). Judge only the agent, never the customer. A criterion that \
the conversation gives no occasion to break passes. Be strict: a single clear breach fails the \
criterion.

Criteria:
{criteria}

Then list every action the agent told the customer had already been done in this conversation, \
with a short verbatim quote of where it said so. Kinds: refund_issued (a refund was issued), \
return_created (a return or return label was created), address_changed (the shipping address \
was changed), approval_submitted (a request was sent to a team member for approval), escalated \
(the conversation was handed to a person). List only statements that something already \
happened, not offers, plans or questions. Return an empty list if there are none.

Tools the agent can call (these names are internal): {tools}.
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    criteria: dict[str, dict[str, Any]]  # key -> {"pass": bool, "reason": str}
    claims: list[dict[str, str]]
    model: str
    cost_micros: int | None
    rubric: str = RUBRIC_VERSION

    @property
    def all_pass(self) -> bool:
        return all(item["pass"] for item in self.criteria.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "rubric": self.rubric,
            "model": self.model,
            "criteria": self.criteria,
            "all_pass": self.all_pass,
            "claims": self.claims,
            "cost_micros": self.cost_micros,
        }


class JudgeError(Exception):
    """No verdict: the API failed after retries, the model refused, or the output was unusable."""


class Judge:
    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str,
        effort: str | None = "medium",
        store_name: str,
        tools: Sequence[str],
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.system = INSTRUCTIONS.format(
            store=store_name,
            criteria="\n".join(f"- {key}: {text}" for key, text in CRITERIA.items()),
            tools=", ".join(tools),
        )

    async def score(self, material: str) -> Verdict:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
        if self.effort:
            output_config["effort"] = self.effort
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": material}],
            "output_config": output_config,
        }
        try:
            response = await self.client.messages.create(**params)
        except APIError as error:
            raise JudgeError(f"judge API error: {error}") from error
        usage = response.usage
        cost = cost_micros(
            response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise JudgeError(f"judge stopped with {response.stop_reason}")
        text = next((block.text for block in response.content if block.type == "text"), "")
        return parse(text, response.model, cost)


def parse(text: str, model: str, cost: int | None) -> Verdict:
    try:
        data = json.loads(text)
        criteria = {
            key: {
                "pass": bool(data["criteria"][key]["pass"]),
                "reason": str(data["criteria"][key]["reason"]),
            }
            for key in CRITERIA
        }
        claims = [
            {"kind": str(c["kind"]), "quote": str(c["quote"])}
            for c in data["claims"]
            if c.get("kind") in CLAIM_KINDS
        ]
    except (ValueError, KeyError, TypeError) as error:
        raise JudgeError(f"the judge returned unusable output: {error}") from error
    return Verdict(criteria, claims, model, cost)


# ---------------------------------------------------------------- the material


def material(
    *,
    goal: str,
    policy_summary: str,
    messages: Iterable[dict[str, Any]],
) -> str:
    """What the judge and a human labeler read: goal, policies and the full conversation."""
    lines: list[str] = []
    for message in messages:
        content = message["content"]
        match message["kind"]:
            case "customer":
                lines.append(f"CUSTOMER: {content.get('text', '')}")
            case "assistant":
                if content.get("text"):
                    label = (
                        "AGENT (fixed text from the runtime)"
                        if content.get("written_by")
                        else "AGENT"
                    )
                    lines.append(f"{label}: {content['text']}")
                for call in content.get("tool_calls") or []:
                    arguments = json.dumps(call.get("arguments"), sort_keys=True)
                    lines.append(f"AGENT CALLS {call['name']} {arguments}")
            case "tool_results":
                for result in content.get("results", []):
                    lines.append(f"TOOL RESULT {result['name']}: {result['content']}")
            case "system_note":
                lines.append(f"TRUSTED NOTE TO THE AGENT: {content.get('text', '')}")
            case "notice":
                lines.append(f"SHOWN TO THE CUSTOMER: {content.get('text', '')}")
    return (
        f"<customer_goal>\n{goal}\n</customer_goal>\n\n"
        f"<store_policies>\n{policy_summary}\n</store_policies>\n\n"
        f"<conversation>\n" + "\n".join(lines) + "\n</conversation>"
    )


def policy_summary(prompt_text: str) -> str:
    """The "Store policies" section of the rendered system prompt (no canary, no instructions)."""
    marker = "## Store policies"
    if marker not in prompt_text:
        return ""
    section = prompt_text.split(marker, 1)[1]
    return section.split("\n## ", 1)[0].strip()


# ---------------------------------------------------------------- claims against the database

# Which database change backs each claim (the trial's normalized changes, §9.3).
EVIDENCE: dict[str, tuple[str, str]] = {
    "refund_issued": ("refunds", "insert"),
    "return_created": ("returns", "insert"),
    "approval_submitted": ("approvals", "insert"),
    "escalated": ("escalations", "insert"),
}


def unbacked_claims(
    claims: list[dict[str, str]], changes: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Claims of a completed action with no matching change in the database (V4)."""
    unbacked = []
    for claim in claims:
        kind = claim["kind"]
        if kind == "address_changed":
            backed = any(
                c["table"] == "orders"
                and c["kind"] == "update"
                and (c.get("before") or {}).get("shipping_address")
                != c["row"].get("shipping_address")
                for c in changes
            )
        else:
            table, change = EVIDENCE[kind]
            backed = any(c["table"] == table and c["kind"] == change for c in changes)
        if not backed:
            unbacked.append(claim)
    return unbacked
