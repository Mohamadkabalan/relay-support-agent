"""`relay chat`: talk to the agent in a terminal, before the web UI exists.

It runs the same turn loop the API will run, against the seeded database, and prints what a
customer would see plus a few lines the operator console will show later: status labels,
approvals, escalations, guard flags, and each turn's tokens and cost.
"""

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable

from relay.agent.loop import Agent, AgentEvent, ConversationClosedError
from relay.agent.store import InboxFullError
from relay.domain.money import format_cents

MAX_MESSAGE_CHARS = 2000  # the API enforces the same limit (§5.7)

ReadLine = Callable[[str], Awaitable[str | None]]
Write = Callable[[str], None]

HELP = "Type a message; /quit (or Ctrl-D) leaves."


class TerminalView:
    """Prints agent events as they arrive."""

    def __init__(self, write: Write) -> None:
        self.write = write
        self.streaming = False

    def line(self, text: str) -> None:
        self.end_stream()
        self.write(text + "\n")

    def end_stream(self) -> None:
        if self.streaming:
            self.write("\n")
            self.streaming = False

    async def on_event(self, event: AgentEvent) -> None:
        payload = event.payload
        match event.type:
            case "message.delta":
                if not self.streaming:
                    self.write("agent> ")
                    self.streaming = True
                self.write(str(payload["text"]))
            case "message.completed":
                if "notice" in payload:
                    self.line(f"agent> {payload['text']}")
                else:
                    self.end_stream()
            case "message.retracted":
                replacement = payload["replacement"]
                self.line(f"  [withheld by the output guard; the customer sees] {replacement}")
            case "status":
                self.line(f"  · {payload['label']}")
            case "approval.pending":
                self.line(f"  [waiting for an operator: {payload['approval_number']}]")
            case "conversation.escalated":
                self.line(f"  [handed to a human: {payload['escalation_number']}]")
            case "guard.input":
                self.line(f"  [input flagged: {', '.join(payload['families'])}]")


async def read_stdin(prompt: str) -> str | None:
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return None


async def run_chat(
    agent: Agent,
    customer_id: uuid.UUID,
    view: TerminalView,
    *,
    read_line: ReadLine = read_stdin,
) -> uuid.UUID:
    conversation_id = await agent.start_conversation(customer_id)
    name = await agent.store.customer_name(customer_id)
    view.line(
        f"Relay chat as {name}, {agent.provider.name} {agent.provider.model} "
        f"(conversation {conversation_id}). {HELP}"
    )
    while True:
        text = await read_line("you> ")
        if text is None or text.strip() == "/quit":
            break
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            view.line(f"  [messages are limited to {MAX_MESSAGE_CHARS} characters]")
            continue
        try:
            outcome = await agent.send(conversation_id, text)
        except (ValueError, InboxFullError) as error:
            view.line(f"  [{error}]")
            continue
        except ConversationClosedError:
            view.line("  [this conversation is closed]")
            break
        if outcome is not None:
            await summarize(agent, conversation_id, view)
    view.end_stream()
    return conversation_id


async def summarize(agent: Agent, conversation_id: uuid.UUID, view: TerminalView) -> None:
    turn = await agent.store.last_turn(conversation_id)
    if turn is None:
        return
    cost = format_cents(0)
    if turn.cost_micros:
        cost = f"${turn.cost_micros / 1_000_000:.4f}"
    processed = turn.input_tokens + turn.output_tokens + turn.cache_read_tokens
    processed += turn.cache_write_tokens
    view.line(
        f"  [turn {turn.seq} {turn.status.value} · {turn.steps} model calls · "
        f"{turn.tool_call_count} tool calls · {processed:,} tokens "
        f"({turn.cache_read_tokens:,} cache reads, {turn.cache_write_tokens:,} cache writes) · "
        f"{cost}]"
    )


def stdout_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
