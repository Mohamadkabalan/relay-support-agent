"""The turn loop (PLAN.md §5.1, §5.2, §5.7): one customer turn from inbox to reply.

    lock -> claim inbox -> input guards -> append inputs and trusted notes
         -> [ model call -> append reply -> run tools -> append results ]*  -> finalize

A turn ends in exactly one terminal state:

| status           | when                                                   | customer sees     |
|------------------|--------------------------------------------------------|-------------------|
| completed        | the model ended its turn                               | the reply         |
| step_limit       | `max_steps` model calls in one turn                    | a handoff         |
| budget_exhausted | conversation token budget used up, or turn deadline    | a handoff         |
| refused          | a refusal that survived the server-side fallback       | a polite decline  |
| failed           | an API error after retries, or output truncated twice  | an apology        |

Step limits and budgets escalate the conversation. Whatever happens, the transcript stays valid
for the next request, and nothing already stored is ever changed:

- a reply that asked for tools always gets its results appended (marked interrupted if the turn
  ended first);
- a turn that ends without a model reply appends what the customer saw (the handoff, decline,
  apology or acknowledgement) as an assistant message, so the model's transcript matches the
  chat and the next customer message never directly follows a system note.

A failed turn is retried by sending the message again, as the apology asks.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from relay.agent.guardrails.injection import injection_families, injection_note
from relay.agent.guardrails.output import RETRACTION_TEXT, order_numbers, output_problems
from relay.agent.locks import Locks
from relay.agent.manifest import Manifest, digest
from relay.agent.prompts import SystemPrompt, canary, system_prompt
from relay.agent.providers.base import (
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    Stop,
    TextDelta,
    ToolCall,
    ToolUseStarted,
)
from relay.agent.store import ConversationStore, Inbound, Pinned, StartedTurn
from relay.agent.tools import ToolConnector, ToolSession, TurnCalls, execute
from relay.agent.transcript import (
    AssistantText,
    CustomerText,
    EventLine,
    Item,
    SystemNote,
    ToolResult,
    ToolResults,
)
from relay.clock import Clock
from relay.db.models import Conversation
from relay.domain.enums import (
    ApprovalStatus,
    ConversationStatus,
    EscalationPriority,
    EscalationReason,
    InboxKind,
    MessageKind,
    TurnStatus,
)
from relay.domain.money import format_cents
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope

logger = logging.getLogger(__name__)

HANDOFF_TEXT = (
    "This needs one of our specialists, so I've passed your case on. They'll reply here {when}."
)
REFUSAL_TEXT = (
    "I'm sorry, but I can't help with that. Is there something about your orders I can help with?"
)
FAILURE_TEXT = "Sorry, something went wrong on our side. Please try sending that again."
ESCALATED_ACK_TEXT = "Thanks, I've added that to your case. Our team will reply here {when}."
CLOSED_TEXT = (
    "This conversation has reached its length limit, so I've closed it. Please start a new "
    "conversation if you need more help."
)
WRAP_UP_NOTE = (
    "This conversation is close to its length limit. Wrap up: resolve the current request, or "
    "hand the conversation to a human if it can't be resolved soon."
)
INTERRUPTED_RESULT = (
    "[INTERRUPTED] The turn ended before this call finished, so its outcome is unknown. Look "
    "the order up again before you assume anything about it."
)


@dataclass(frozen=True)
class AgentLimits:
    """The safeguards of §5.7."""

    max_steps: int = 8  # model calls per turn
    max_tool_calls: int = 16  # tool calls per turn
    turn_deadline_s: float = 90.0
    conversation_tokens: int = 500_000  # processed tokens, see Usage.processed
    wrap_up_at: float = 0.85  # share of the token budget that triggers the wrap-up note
    max_tokens: int = 16_000
    max_tokens_retry: int = 32_000  # one retry after a truncated response
    tool_timeout_s: float = 10.0
    max_customer_messages: int = 40
    max_injection_flags: int = 3
    max_waiting_messages: int = 3

    @property
    def lock_ttl_s(self) -> float:
        return self.turn_deadline_s + 30


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str
    payload: dict[str, Any]


Listener = Callable[[AgentEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn_id: uuid.UUID
    seq: int
    status: TurnStatus
    replies: list[str]  # what the customer saw, in order (replies and notices)
    escalation_number: str | None = None


class TurnInProgressError(Exception):
    """Another turn holds this conversation's lock; the new input waits in the inbox."""


class ConversationClosedError(Exception):
    pass


class DeadlineReachedError(Exception):
    pass


class TruncatedTwiceError(Exception):
    pass


@dataclass
class Agent:
    """Everything a turn needs. One instance serves many conversations of one provider."""

    store: ConversationStore
    provider: ModelProvider
    tools: ToolConnector
    locks: Locks
    clock: Clock
    policy: PolicyConfig
    store_name: str = "Pixel & Volt"
    limits: AgentLimits = field(default_factory=AgentLimits)
    user_key_secret: str | None = None  # keys the opaque metadata.user_id; None sends none
    listener: Listener | None = None  # live events, including text deltas (never stored)

    async def start_conversation(self, customer_id: uuid.UUID) -> uuid.UUID:
        """Create a conversation, pinning the provider, prompt and tool manifest (§5.4)."""
        conversation_id = uuid.uuid7()
        scope = CustomerScope(customer_id, conversation_id=conversation_id, turn_seq=1)
        async with self.tools.connect(scope) as session:
            manifest = await session.list_tools()
        manifest_text = manifest.to_json()
        prompt = system_prompt(self.policy, self.store_name)
        pinned = Pinned(
            provider=self.provider.name,
            model=self.provider.model,
            effort=self.provider.effort,
            prompt_version=prompt.version,
            prompt_hash=prompt.hash,
            system_prompt=prompt.text,
            tool_manifest=manifest_text,
            manifest_hash=digest(manifest_text),
            policy_version=self.policy.version,
        )
        return await self.store.create(customer_id, pinned, conversation_id)

    async def send(self, conversation_id: uuid.UUID, text: str) -> TurnOutcome | None:
        """Queue a customer message and run the turn that answers it."""
        await self.store.enqueue_message(
            conversation_id, text, max_waiting=self.limits.max_waiting_messages
        )
        return await self.run_turn(conversation_id)

    async def run_turn(self, conversation_id: uuid.UUID) -> TurnOutcome | None:
        """Run one turn for whatever is waiting in the inbox. None if nothing was waiting."""
        key = f"conversation:{conversation_id}"
        async with self.locks.hold(key, self.limits.lock_ttl_s) as held:
            if not held:
                raise TurnInProgressError(str(conversation_id))
            conversation = await self.store.get(conversation_id)
            if conversation.status is ConversationStatus.CLOSED:
                raise ConversationClosedError(str(conversation_id))
            if (conversation.provider, conversation.model) != (
                self.provider.name,
                self.provider.model,
            ):
                raise ValueError(
                    f"Conversation {conversation_id} is pinned to {conversation.provider} "
                    f"{conversation.model}; start a new conversation to switch."
                )
            started = await self.store.start_turn(conversation_id)
            if started is None:
                return None
            return await _TurnRun(self, conversation, started).run()


class _TurnRun:
    """The state of one turn while it runs."""

    def __init__(self, agent: Agent, conversation: Conversation, started: StartedTurn) -> None:
        self.agent = agent
        self.store = agent.store
        self.limits = agent.limits
        self.conversation = conversation
        self.id = conversation.id
        self.turn = started
        self.manifest = Manifest.from_json(conversation.tool_manifest)
        self.prompt = SystemPrompt(
            version=conversation.prompt_version,
            text=conversation.system_prompt,
            hash=conversation.prompt_hash,
            canary=canary(conversation.prompt_version),
        )
        self.scope = CustomerScope(
            conversation.customer_id, conversation_id=conversation.id, turn_seq=started.seq
        )
        self.started_at = time.monotonic()
        self.deadline = self.started_at + agent.limits.turn_deadline_s
        self.processed = conversation.processed_tokens
        self.budget_warned = conversation.budget_warned
        self.model_calls = 0
        self.tool_seq = 0
        self.calls = TurnCalls(agent.limits.max_tool_calls, agent.limits.tool_timeout_s)
        self.pending: list[ToolCall] = []  # tool calls appended without their results yet
        self.replies: list[str] = []
        self.retracted = False
        self.escalation_number: str | None = None
        self.exits = AsyncExitStack()
        self.session: ToolSession | None = None

    # ------------------------------------------------------------ the turn

    async def run(self) -> TurnOutcome:
        await self.emit("turn.started", {"turn": self.turn.seq, "trigger": self.turn.trigger})
        error: str | None = None
        try:
            status = await self.body()
        except DeadlineReachedError:
            status, error = TurnStatus.BUDGET_EXHAUSTED, "turn deadline reached"
        except TruncatedTwiceError:
            status, error = TurnStatus.FAILED, "output truncated twice"
        except ProviderError as failure:
            status, error = TurnStatus.FAILED, str(failure)
        except Exception as failure:
            logger.exception("Turn %s of conversation %s crashed", self.turn.seq, self.id)
            status, error = TurnStatus.FAILED, f"{type(failure).__name__}: {failure}"
        finally:
            await self.exits.aclose()
        return await self.finish(status, error)

    async def body(self) -> TurnStatus:
        texts = [
            i.payload["text"] for i in self.turn.inputs if i.kind is InboxKind.CUSTOMER_MESSAGE
        ]
        approvals = [i for i in self.turn.inputs if i.kind is InboxKind.APPROVAL_RESOLVED]

        if self.conversation.status is ConversationStatus.ESCALATED and texts:
            # A human owns the conversation now: acknowledge without a model call (§5.3).
            notes = [_approval_note(approval, self.agent.policy) for approval in approvals]
            await self.append_inputs(texts, approvals, notes)
            when = self.agent.policy.escalation.response_time_text
            await self.say(ESCALATED_ACK_TEXT.format(when=when), "escalated")
            return TurnStatus.COMPLETED

        earlier = len(await self.store.customer_texts(self.id))
        if earlier + len(texts) > self.limits.max_customer_messages:
            await self.append_inputs(texts, approvals, notes=[])
            await self.notice(CLOSED_TEXT, "length_limit")
            await self.store.set_status(self.id, ConversationStatus.CLOSED)
            return TurnStatus.COMPLETED

        notes, flagged = await self.trusted_notes(texts, approvals)
        await self.append_inputs(texts, approvals, notes)
        if flagged >= self.limits.max_injection_flags:
            await self.hand_off(
                EscalationReason.SUSPECTED_FRAUD,
                f"{flagged} customer messages in this conversation looked like attempts to "
                "instruct the agent. Review the transcript.",
                EscalationPriority.HIGH,
            )
            return TurnStatus.COMPLETED
        return await self.model_loop()

    async def model_loop(self) -> TurnStatus:
        while True:
            if self.model_calls >= self.limits.max_steps:
                return TurnStatus.STEP_LIMIT
            if self.processed >= self.limits.conversation_tokens:
                return TurnStatus.BUDGET_EXHAUSTED
            response = await self.call_model(self.limits.max_tokens)
            if response.stop is Stop.MAX_TOKENS:
                if self.model_calls >= self.limits.max_steps:
                    return TurnStatus.STEP_LIMIT
                response = await self.call_model(self.limits.max_tokens_retry)
                if response.stop is Stop.MAX_TOKENS:
                    raise TruncatedTwiceError
            if response.stop is Stop.REFUSAL:
                return TurnStatus.REFUSED
            if response.raw is None:
                raise ProviderError("The model returned an empty response.")
            await self.append_reply(response)
            if response.stop is Stop.END_TURN:
                return TurnStatus.COMPLETED
            await self.run_tools(response.tool_calls)

    async def call_model(self, max_tokens: int) -> ModelResponse:
        request = ModelRequest(
            system=self.conversation.system_prompt,
            tools=self.manifest.tools,
            transcript=await self.store.transcript(self.id),
            max_tokens=max_tokens,
            user_key=self.user_key(),
        )
        self.model_calls += 1
        response = await self.within_deadline(
            self.agent.provider.complete(request, self.on_model_event)
        )
        await self.store.record_model_call(
            self.id,
            self.turn.id,
            self.model_calls,
            provider=self.agent.provider.name,
            effort=self.agent.provider.effort,
            max_tokens=max_tokens,
            response=response,
        )
        self.processed += response.usage.processed
        return response

    async def append_reply(self, response: ModelResponse) -> None:
        content: dict[str, Any] = {
            "text": response.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
            "stop": response.stop.value,
            "model": response.served_model,
        }
        seq = await self.store.append(
            self.id, self.turn.id, MessageKind.ASSISTANT, content, response.raw
        )
        self.pending = list(response.tool_calls)
        if response.text:
            await self.deliver(seq, response.text)

    async def deliver(self, seq: int, text: str) -> None:
        """A completed reply: check it, then show it or replace it."""
        await self.emit("message.completed", {"message_seq": seq, "text": text})
        allowed = await self.store.order_numbers(self.conversation.customer_id)
        for typed in await self.store.customer_texts(self.id):
            allowed |= order_numbers(typed)
        problems = output_problems(text, self.prompt, allowed)
        if not problems:
            self.replies.append(text)
            return
        self.retracted = True
        await self.emit("message.retracted", {"message_seq": seq, "replacement": RETRACTION_TEXT})
        await self.store.flag(
            self.id, {"type": "retraction", "turn": self.turn.seq, "reasons": problems}
        )
        await self.notice(RETRACTION_TEXT, "retraction")

    async def run_tools(self, calls: list[ToolCall]) -> None:
        session = await self.tool_session()
        executed = await self.within_deadline(execute(calls, session, self.manifest, self.calls))
        await self.store.record_tool_calls(self.turn.id, self.tool_seq + 1, executed)
        self.tool_seq += len(executed)
        await self.append(ToolResults(tuple(done.result for done in executed)))
        self.pending = []
        for done in executed:
            await self.after_tool(done.result)
        if not self.budget_warned and self.near_budget():
            await self.append(SystemNote(WRAP_UP_NOTE))
            await self.warned()

    async def after_tool(self, result: ToolResult) -> None:
        """Turn tool outcomes the console must know about into events."""
        if result.is_error:
            return
        try:
            data = json.loads(result.content)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if data.get("status") == "pending_approval":
            await self.emit(
                "approval.pending",
                {"approval_number": data.get("approval_number"), "summary": data.get("summary")},
            )
        if result.name == "escalate_to_human" and data.get("escalation_number"):
            self.escalation_number = str(data["escalation_number"])
            await self.store.set_status(self.id, ConversationStatus.ESCALATED)
            await self.emit(
                "conversation.escalated",
                {
                    "escalation_number": self.escalation_number,
                    "expected_response": data.get("expected_response"),
                },
            )

    # ------------------------------------------------------------ finishing

    async def finish(self, status: TurnStatus, error: str | None) -> TurnOutcome:
        if self.pending:  # keep every tool call paired with a result
            interrupted = tuple(
                ToolResult(call.id, call.name, INTERRUPTED_RESULT, is_error=True)
                for call in self.pending
            )
            await self.append(ToolResults(interrupted))
            self.pending = []
        match status:
            case TurnStatus.STEP_LIMIT | TurnStatus.BUDGET_EXHAUSTED:
                what = "step limit" if status is TurnStatus.STEP_LIMIT else "token or time budget"
                await self.hand_off(
                    EscalationReason.COMPLEX_ISSUE,
                    f"The agent reached its {what} in turn {self.turn.seq} without resolving "
                    "the customer's request.",
                    EscalationPriority.NORMAL,
                )
            case TurnStatus.REFUSED:
                await self.store.flag(self.id, {"type": "refusal", "turn": self.turn.seq})
                await self.say(REFUSAL_TEXT, "refusal")
            case TurnStatus.FAILED:
                await self.store.flag(
                    self.id, {"type": "failure", "turn": self.turn.seq, "error": error}
                )
                await self.say(FAILURE_TEXT, "failure")
                await self.emit("error", {"code": "turn_failed", "message": FAILURE_TEXT})
        await self.store.finish_turn(
            self.turn.id,
            status,
            latency_ms=int((time.monotonic() - self.started_at) * 1000),
            error=error,
            retracted=self.retracted,
        )
        await self.emit("turn.completed", {"turn": self.turn.seq, "status": status.value})
        return TurnOutcome(
            self.turn.id, self.turn.seq, status, self.replies, self.escalation_number
        )

    async def hand_off(
        self, reason: EscalationReason, summary: str, priority: EscalationPriority
    ) -> None:
        escalation = await self.store.escalate(
            self.id, reason=reason, summary=summary, priority=priority
        )
        self.escalation_number = escalation.escalation_number
        await self.emit(
            "conversation.escalated",
            {
                "escalation_number": escalation.escalation_number,
                "expected_response": escalation.expected_response,
            },
        )
        await self.say(HANDOFF_TEXT.format(when=escalation.expected_response), "handoff")

    # ------------------------------------------------------------ inputs and notes

    async def trusted_notes(
        self, texts: list[str], approvals: list[Inbound]
    ) -> tuple[list[str], int]:
        """The system note for this turn's inputs, and the conversation's injection-flag count."""
        notes: list[str] = []
        if self.turn.seq == 1:
            notes.append(await self.context_note())
        previous = await self.store.turns_before(self.id, self.turn.seq)
        if previous and previous[-1].retracted:
            notes.append(
                "Your previous reply was withheld from the customer by an automated check and "
                f'replaced with: "{RETRACTION_TEXT}" Don\'t repeat the withheld content.'
            )
        flagged = self.conversation.injection_flags
        for text in texts:
            families = injection_families(text)
            if families:
                flagged = await self.store.count_injection_flag(self.id)
                notes.append(injection_note(families))
                await self.emit("guard.input", {"families": families})
        notes.extend(_approval_note(approval, self.agent.policy) for approval in approvals)
        if not self.budget_warned and self.near_budget():
            notes.append(WRAP_UP_NOTE)
            await self.warned()
        return notes, flagged

    async def context_note(self) -> str:
        policy = self.agent.policy
        local = self.agent.clock.now().astimezone(policy.tz)
        name = await self.store.customer_name(self.conversation.customer_id)
        return (
            f"Current date and time: {local:%A} {local:%Y-%m-%d}, {local:%H:%M} "
            f"({policy.store_timezone}). Signed-in customer: {name}."
        )

    async def append_inputs(
        self, texts: list[str], approvals: list[Inbound], notes: list[str]
    ) -> None:
        for text in texts:
            await self.append(CustomerText(text))
        if approvals and not texts:
            await self.append(EventLine())
        if notes:
            # One system message: it has to follow a user message (§5.4).
            await self.append(SystemNote("\n\n".join(notes)))

    async def append(self, item: Item) -> int:
        kind, content = _describe(item)
        raw = self.agent.provider.render(item)
        return await self.store.append(self.id, self.turn.id, kind, content, raw)

    async def say(self, text: str, reason: str) -> None:
        """A reply the runtime writes instead of the model. It joins the model's transcript."""
        seq = await self.append(AssistantText(text))
        self.replies.append(text)
        await self.emit("message.completed", {"message_seq": seq, "text": text, "notice": reason})

    async def notice(self, text: str, reason: str) -> None:
        """Shown to the customer, never sent to the model."""
        seq = await self.store.append(
            self.id, self.turn.id, MessageKind.NOTICE, {"text": text, "reason": reason}, None
        )
        self.replies.append(text)
        await self.emit("message.completed", {"message_seq": seq, "text": text, "notice": reason})

    # ------------------------------------------------------------ helpers

    async def tool_session(self) -> ToolSession:
        """Connect to the MCP server on first use, so a turn without tool calls never connects."""
        if self.session is None:
            self.session = await self.exits.enter_async_context(
                self.agent.tools.connect(self.scope)
            )
        return self.session

    async def within_deadline[T](self, work: Awaitable[T]) -> T:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineReachedError
        try:
            async with asyncio.timeout(remaining):
                return await work
        except TimeoutError as error:
            raise DeadlineReachedError from error

    async def on_model_event(self, event: ModelEvent) -> None:
        match event:
            case TextDelta(text):
                await self.live("message.delta", {"text": text})
            case ToolUseStarted(name):
                tool = self.manifest.get(name)
                label = tool.status_label if tool else "Working on it…"
                await self.emit("status", {"label": label})

    async def emit(self, type_: str, payload: dict[str, Any]) -> None:
        await self.store.emit(self.id, self.turn.id, type_, payload)
        await self.live(type_, payload)

    async def live(self, type_: str, payload: dict[str, Any]) -> None:
        if self.agent.listener is not None:
            await self.agent.listener(AgentEvent(type_, payload))

    def near_budget(self) -> bool:
        return self.processed >= self.limits.wrap_up_at * self.limits.conversation_tokens

    async def warned(self) -> None:
        self.budget_warned = True
        await self.store.mark_budget_warned(self.id)

    def user_key(self) -> str | None:
        secret = self.agent.user_key_secret
        if secret is None:
            return None
        key = hmac.new(secret.encode(), b"relay metadata.user_id", hashlib.sha256).digest()
        customer = str(self.conversation.customer_id).encode()
        return hmac.new(key, customer, hashlib.sha256).hexdigest()[:32]


def _describe(item: Item) -> tuple[MessageKind, dict[str, Any]]:
    match item:
        case CustomerText(text):
            return MessageKind.CUSTOMER, {"text": text}
        case EventLine(text):
            return MessageKind.EVENT, {"text": text}
        case AssistantText(text):
            return MessageKind.ASSISTANT, {"text": text, "written_by": "runtime"}
        case SystemNote(text):
            return MessageKind.SYSTEM_NOTE, {"text": text}
        case ToolResults(results):
            return MessageKind.TOOL_RESULTS, {
                "results": [
                    {
                        "call_id": result.call_id,
                        "name": result.name,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                    for result in results
                ]
            }


def _approval_note(approval: Inbound, policy: PolicyConfig) -> str:
    """The authoritative facts about an operator decision, for the resumed turn (§5.9)."""
    data = approval.payload
    number = data.get("approval_number")
    what = data.get("summary")
    amount = data.get("amount_cents")
    if isinstance(amount, int):
        what = f"{what} ({format_cents(amount)})"
    subject = f"Update on request {number} for order {data.get('order_number')}: {what}."
    match data.get("status"):
        case ApprovalStatus.EXECUTED.value:
            result = (
                "A team member approved it and it has been carried out. "
                f"Result: {data.get('outcome')}."
            )
        case ApprovalStatus.REJECTED.value:
            result = "A team member declined it."
            if data.get("note"):
                result += f" Their note: {data['note']}"
        case ApprovalStatus.FAILED.value:
            result = (
                "A team member approved it, but it could not be carried out because the order "
                f"changed: {data.get('outcome')}."
            )
        case ApprovalStatus.EXPIRED.value:
            hours = policy.approvals.expire_after_hours
            result = (
                f"No decision was made within {hours} hours, so it expired and the case was "
                "passed to our team."
            )
        case other:
            result = f"Its status is now {other}."
    return (
        f"{subject} {result} Tell the customer what happened. If it was declined or failed, "
        "explain it and offer what you can still do."
    )
