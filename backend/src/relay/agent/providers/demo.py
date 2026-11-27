"""A rule-based stand-in for the model (`RELAY_PROVIDER=scripted`): the key-free demo and the
browser smoke test.

It is not an agent. It reads the transcript the loop sends, the same way the real model would
receive it, and follows one story through the real MCP tools: a damaged item, a refund preview,
the customer's yes, the commit (with an operator approval over $100), and the outcome. It also
hands off when asked for a human. Everything else gets a short note saying what it can show.

Because it runs the production loop, tools, policy, approvals and events, the browser test that
drives it exercises everything except the model.
"""

import dataclasses
import json
import re
from typing import Any

from relay.agent.providers.base import EventSink, ModelRequest, ModelResponse
from relay.agent.providers.scripted import Call, Reply, ScriptedProvider, ScriptedRequest
from relay.agent.transcript import EVENT_LINE
from relay.domain.money import format_cents

MODEL = "scripted-demo"

HELP_TEXT = (
    "Hi! I'm the scripted demo assistant: no AI model is connected right now. I can show you "
    "one thing end to end: a refund for an item that arrived damaged. Tell me what arrived "
    "broken, or ask for a human."
)
REFUND_WORDS = re.compile(r"\b(refund|broken|broke|cracked|damaged|defective|faulty)\b", re.I)
HUMAN_WORDS = re.compile(r"\b(human|person|agent|representative|someone)\b", re.I)
YES_WORDS = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|go ahead|please do|confirm)\b", re.I)


class DemoProvider(ScriptedProvider):
    name = "scripted"

    def __init__(self) -> None:
        super().__init__([], model=MODEL)

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        self.steps = [decide]
        response = await super().complete(request, on_event)
        self.requests.clear()  # a long-running server must not keep every request
        return dataclasses.replace(response, cost_micros=0)  # free: settles the spend cap


def decide(sent: ScriptedRequest) -> Reply:
    messages = sent.messages
    last = messages[-1]
    if last["role"] == "user" and _tool_results(last):
        return _after_tool(messages, _tool_results(last)[-1])
    if last["role"] == "system" and _is_event(messages[-2]):
        return Reply(text=_approval_text(str(last["content"])))
    text = _customer_text(messages)
    if HUMAN_WORDS.search(text):
        summary = "The customer asked for a person (scripted demo)."
        args = {"reason": "customer_requested", "summary": summary, "priority": "normal"}
        return Reply(calls=(Call("escalate_to_human", args),))
    preview = _open_preview(messages)
    if preview is not None and YES_WORDS.search(text):
        return Reply(calls=(Call("issue_refund", preview),))
    if REFUND_WORDS.search(text):
        return Reply(calls=(Call("list_orders", {}),))
    return Reply(text=HELP_TEXT)


def _after_tool(messages: list[Any], result: dict[str, Any]) -> Reply:
    call = _call_for(messages, result["tool_use_id"])
    name = call["name"] if call else ""
    if result.get("is_error"):
        return Reply(text=f"Sorry, that didn't work: {result['content']}")
    data = json.loads(result["content"])
    if name == "list_orders":
        delivered = [o for o in data["orders"] if o["status"] == "delivered"]
        if not delivered:
            return Reply(text="I don't see a delivered order on your account to refund.")
        order = delivered[0]["number"]
        args = {
            "order_number": order,
            "reason": "damaged_or_defective",
            "lines": [{"line": 1, "quantity": 1}],
        }
        return Reply(calls=(Call("issue_refund", args),))
    if name == "escalate_to_human":
        when = data.get("expected_response", "soon")
        return Reply(
            text=f"I've passed your case to one of our specialists. They'll reply here {when}."
        )
    match data.get("status"):
        case "preview":
            review = (
                " It needs a quick review by our team first."
                if data["needs_operator_approval"]
                else ""
            )
            return Reply(
                text=f"I can do this: {data['summary'].rstrip('.')}.{review} Shall I go ahead?"
            )
        case "pending_approval":
            return Reply(
                text=f"Submitted for review as {data['approval_number']}. You'll see the decision "
                "right here."
            )
        case "completed":
            amount = format_cents(int(data["amount_cents"]))
            return Reply(
                text=f"Done: {amount} is on its way to your {data['destination']} "
                f"({data['settles_in']}). Your refund number is {data['refund_number']}."
            )
        case "denied":
            return Reply(text=f"I'm sorry, I can't do that: {data['explanation']}")
    return Reply(text=HELP_TEXT)


def _approval_text(note: str) -> str:
    if "approved it and it has been carried out" in note:
        return "Good news: our team approved your refund, and it's on its way."
    if "declined" in note:
        reason = note.split("Their note:", 1)[1].split("Tell the customer", 1)[0].strip()
        return f"I'm sorry, our team declined the request. {reason}".strip()
    if "expired" in note:
        return "Your request wasn't reviewed in time, so I've passed it to our team to follow up."
    return "There's an update on your request: it could not be carried out as it was."


# ---------------------------------------------------------------- reading the transcript


def _tool_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message["content"]
    if not isinstance(content, list):
        return []
    return [block for block in content if block.get("type") == "tool_result"]


def _is_event(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        block.get("type") == "text" and block.get("text") == EVENT_LINE for block in content
    )


def _customer_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if message["role"] != "user" or _tool_results(message) or _is_event(message):
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        return " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    return ""


def _call_for(messages: list[Any], tool_use_id: str) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message["role"] != "assistant":
            continue
        for block in message["content"]:
            if block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                return dict(block)
    return None


def _open_preview(messages: list[Any]) -> dict[str, Any] | None:
    """The arguments of the newest refund preview not committed yet, plus its token."""
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        for result in reversed(_tool_results(message)):
            call = _call_for(messages, result["tool_use_id"])
            if call is None or call["name"] != "issue_refund" or result.get("is_error"):
                continue
            data = json.loads(result["content"])
            if data.get("status") != "preview":
                return None  # the newest refund call already committed
            return {**call["input"], "confirmation_token": data["confirmation_token"]}
    return None
