"""Eval scenarios drafted from real conversations (§10, the feedback loop).

An operator presses "Create eval scenario" on a conversation and writes what should have
happened. Claude gets the transcript, the tool results the agent saw and that note, and returns
a scenario. The draft is valid only if:

- it parses and passes the same validation as `evals/scenarios/*.yaml` (fixtures, references,
  SKUs, placeholders), and
- none of the real customer's details survive: name, email, phone, address lines, postal code,
  or any email or phone number at all outside the synthetic `eval.example` domain.

The scenario format has free-form maps (`expect.db` rows, tool arguments) that strict
structured outputs can't express, so the model returns the YAML inside a small JSON envelope
(`scenario_yaml`, `rationale`) and our validator judges the YAML.

`relay eval import-drafts` writes valid drafts to `evals/scenarios/drafts/`, which the loader
doesn't read; a human moves a reviewed draft into the suite.
"""

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from anthropic import AsyncAnthropic
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.agent.pricing import cost_micros
from relay.db.models import Address, Conversation, Customer, Message
from relay.domain.enums import MessageKind
from relay.evals.scenario import Scenario
from relay.telemetry.redaction import PATTERNS

MAX_TOKENS = 16_000
# A copy of evals/scenarios/happy-refund-damaged-under-limit.yaml shipped with the package (the API
# image has no evals/ directory); a test keeps the two identical.
EXAMPLE = Path(__file__).with_name("draft_example.yaml")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenario_yaml": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["scenario_yaml", "rationale"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You write eval scenarios for Relay, a customer-support agent for an online electronics store.
You get a real conversation between a customer and the agent, the tool results the agent saw,
and an operator's note saying what should have happened. Write one scenario that reproduces the
situation, so the eval suite checks the behavior the operator wants.

Rules:
- Replace every real name, email address, phone number and street address with invented ones.
  Customer emails may be left out (the harness generates them); if you give one, use the
  domain eval.example. Never copy a real person's details.
- Every customer and order the scenario needs comes from its fixtures. Keep the SKUs, prices,
  order statuses and day counts the agent saw, so the policy outcome is the same. Write order
  numbers as placeholders like {o1.number}, never as real numbers.
- The simulated customer's goal, facts and first message come from what the real customer
  wanted. Put details the customer only gives when asked into hidden_facts.
- expect.db lists the rows the right outcome creates or changes, as partial matches on the
  tables customers, addresses, orders, shipments, returns, refunds, approvals, escalations. If
  nothing may change, leave it empty. A scenario that expects changes needs oracle steps: the
  tool calls that do the right thing directly.
- Pick the category that fits: happy_path, policy_edge, multiple_requests, change_of_mind,
  upset_customer, not_entitled, other_customers, hitl, escalation.
- Use an id in kebab-case starting with "draft-".

The format, by example (a complete, valid scenario):

{example}

Return the scenario as YAML in scenario_yaml, and in rationale one or two sentences on what it
tests and why.
"""


@dataclass(frozen=True)
class DraftResult:
    yaml_text: str
    rationale: str
    problems: list[str]
    model: str
    cost_micros: int | None


async def conversation_material(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[str, list[str]]:
    """The conversation as the drafter sees it, and the customer's real details to keep out."""
    conversation = await session.get_one(Conversation, conversation_id)
    customer = await session.get_one(Customer, conversation.customer_id)
    addresses = list(
        await session.scalars(select(Address).where(Address.customer_id == customer.id))
    )
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    lines: list[str] = []
    for row in rows:
        content = row.content
        match row.kind:
            case MessageKind.CUSTOMER:
                lines.append(f"CUSTOMER: {content.get('text', '')}")
            case MessageKind.ASSISTANT:
                if content.get("text"):
                    lines.append(f"AGENT: {content['text']}")
                for call in content.get("tool_calls") or []:
                    lines.append(f"AGENT CALLS {call['name']} {json.dumps(call.get('arguments'))}")
            case MessageKind.TOOL_RESULTS:
                for result in content.get("results", []):
                    lines.append(f"TOOL RESULT {result['name']}: {result['content']}")
            case MessageKind.SYSTEM_NOTE:
                lines.append(f"TRUSTED NOTE TO THE AGENT: {content.get('text', '')}")
            case MessageKind.NOTICE:
                lines.append(f"SHOWN TO THE CUSTOMER: {content.get('text', '')}")
    pii = [customer.full_name, *customer.full_name.split(), customer.email]
    if customer.phone:
        pii.append(customer.phone)
    for address in addresses:
        pii += [address.line1, address.postal_code]
        if address.line2:
            pii.append(address.line2)
    when = conversation.created_at.date().isoformat()
    return f"Conversation started {when}.\n\n" + "\n".join(lines), [p for p in pii if len(p) >= 3]


def check(yaml_text: str, pii: list[str]) -> list[str]:
    """Everything wrong with a draft; empty means valid."""
    problems = []
    lowered = yaml_text.lower()
    for value in pii:
        if value.lower() in lowered:
            problems.append(f"contains a real customer detail ({_hint(value)})")
    for pattern, mask in PATTERNS:
        if mask not in {"[email]", "[phone]"}:
            continue
        for found in pattern.findall(yaml_text):
            if mask == "[email]" and found.lower().endswith("@eval.example"):
                continue
            problems.append(
                f"contains an {'email address' if mask == '[email]' else 'phone number'}"
            )
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as error:
        return [*problems, f"not valid YAML: {error}"]
    if not isinstance(data, dict):
        return [*problems, "the YAML is not a mapping"]
    try:
        Scenario.model_validate(data)
    except ValidationError as error:
        problems += [
            f"{'.'.join(map(str, e['loc'])) or 'scenario'}: {e['msg']}" for e in error.errors()
        ]
    return list(dict.fromkeys(problems))


def _hint(value: str) -> str:
    return value[:2] + "…" if len(value) > 2 else "…"


async def draft(
    client: AsyncAnthropic, model: str, material: str, note: str, pii: list[str]
) -> DraftResult:
    # Not str.format: the example is full of {ref.field} placeholders.
    system = INSTRUCTIONS.replace("{example}", EXAMPLE.read_text("utf-8").strip())
    prompt = (
        f"<conversation>\n{material}\n</conversation>\n\n<operator_note>\n{note}\n</operator_note>"
    )
    response = await client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    )
    text = next((block.text for block in response.content if block.type == "text"), "{}")
    usage = response.usage
    cost = cost_micros(
        response.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )
    try:
        parsed = json.loads(text)
        yaml_text, rationale = str(parsed["scenario_yaml"]), str(parsed.get("rationale", ""))
    except ValueError, KeyError:
        return DraftResult("", "", ["the model did not return a scenario"], response.model, cost)
    yaml_text = re.sub(r"^```(?:yaml)?\n|\n```$", "", yaml_text.strip()) + "\n"
    return DraftResult(yaml_text, rationale, check(yaml_text, pii), response.model, cost)
