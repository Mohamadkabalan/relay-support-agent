"""One trial (PLAN.md §9.1): a fresh database, the real agent, a simulated customer, a grade.

1. Clone the template, apply the scenario's fixtures, snapshot the changeable tables.
2. Serve the MCP app for this database in this process, behind the same per-turn JWT check as
   production. The agent reaches it over Streamable HTTP through an in-process ASGI transport:
   the same app and protocol, without allocating a port per trial.
3. Run the conversation: the customer's message, the agent's turn, the operator stub for any
   approval (with the resume turn the API's scheduler would run), then the simulator's next
   message. Stop when the simulator is done, the conversation is escalated or closed, or the
   customer-turn limit is reached. The whole trial has a wall-clock ceiling.
4. Snapshot again, re-check committed writes against the policy on a fresh copy of the pre-trial
   state, grade, and write the transcript.

Failures that say nothing about the agent raise `TrialAbortedError` (an API that stayed unavailable
after retries, the wall-clock ceiling, a bug in the harness). The runner records those in
`errors.jsonl` instead of counting them as failed trials.
"""

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.agent.guardrails.output import order_numbers
from relay.agent.locks import LocalLocks
from relay.agent.loop import Agent, AgentLimits, TurnOutcome
from relay.agent.manifest import Manifest
from relay.agent.prompts import canary
from relay.agent.providers.base import ModelProvider
from relay.agent.store import ConversationStore
from relay.agent.tools import HttpTools
from relay.clock import FrozenClock
from relay.config import Settings
from relay.db.models import (
    Approval,
    Conversation,
    Message,
    ModelCall,
    Order,
    ToolCall,
    Turn,
)
from relay.db.session import create_engine, make_sessionmaker
from relay.domain import approvals
from relay.domain.enums import ApprovalStatus, ConversationStatus, MessageKind
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import Deps, OperatorScope
from relay.evals import databases
from relay.evals.databases import EVAL_NOW
from relay.evals.fixtures import Applied, apply_fixtures
from relay.evals.grading import TrialRecord, committed_writes, grade, recheck_writes
from relay.evals.scenario import Scenario, fill
from relay.evals.simulator import Simulator, SimulatorUnavailableError
from relay.evals.snapshot import diff, snapshot
from relay.kb.embeddings import Embedder
from relay.mcp_server.identity import TokenIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import create_http_app


class TrialAbortedError(Exception):
    """The trial produced no gradable result. `kind`: infrastructure, wall_clock or harness."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class RunSetup:
    """What every trial of a run shares."""

    settings: Settings  # needs mcp_jwt_secret; the store name comes from here too
    server_url: str  # the Postgres server, for cloning
    template: str
    policy: PolicyConfig
    embedder: Embedder
    make_provider: Callable[[], ModelProvider]
    make_simulator: Callable[[Scenario, Applied], Simulator]
    limits: AgentLimits = field(default_factory=AgentLimits)
    wall_clock_s: float = 600.0
    transcripts: Path | None = None
    prompt_transform: Callable[[str], str] | None = None
    manifest_transform: Callable[[Manifest], Manifest] | None = None
    clone_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def secret(self) -> str:
        if self.settings.mcp_jwt_secret is None:
            raise RuntimeError("Set RELAY_MCP_JWT_SECRET: trials call the MCP app with tokens.")
        return self.settings.mcp_jwt_secret.get_secret_value()


async def run_trial(setup: RunSetup, scenario: Scenario, trial: int) -> dict[str, Any]:
    started = time.monotonic()
    clone_started = time.monotonic()
    async with setup.clone_lock:  # one CREATE DATABASE ... TEMPLATE at a time
        name, url = await databases.clone(setup.server_url, setup.template)
    clone_ms = int((time.monotonic() - clone_started) * 1000)
    engine = create_engine(url, pool_size=5)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session, session.begin():
            applied = await apply_fixtures(session, scenario, EVAL_NOW, setup.policy)
        async with sessionmaker() as session:
            before = await snapshot(session)
        try:
            async with asyncio.timeout(setup.wall_clock_s):
                conversation = await _converse(setup, scenario, applied, sessionmaker)
        except TimeoutError as error:
            raise TrialAbortedError("wall_clock", f"over {setup.wall_clock_s:.0f} s") from error
        record, evidence = await _collect(
            setup, scenario, trial, applied, sessionmaker, before, conversation
        )
    except TrialAbortedError:
        raise
    except Exception as error:  # a bug in the harness or the fixtures: never a failed trial
        aborted = _aborted(error)
        if aborted is not None:
            raise aborted from error
        raise TrialAbortedError("harness", f"{type(error).__name__}: {error}") from error
    finally:
        await engine.dispose()
        await databases.drop(setup.server_url, name)

    if record.changes:
        record.policy_denials = await _recheck(setup, scenario, evidence["writes"])
    graded = grade(record)
    row = _row(scenario, trial, record, graded, evidence, conversation)
    row["clone_ms"] = clone_ms
    row["wall_s"] = round(time.monotonic() - started, 2)
    if setup.transcripts is not None:
        _write_transcript(setup.transcripts, row, evidence, conversation)
    return row


def _aborted(error: BaseException) -> TrialAbortedError | None:
    """The abort inside an exception group (the MCP app's task group wraps what it sees)."""
    if isinstance(error, TrialAbortedError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            found = _aborted(inner)
            if found is not None:
                return found
    return None


@dataclass
class _Conversation:
    id: Any
    stop_reason: str
    visible: list[str] = field(default_factory=list)
    customer_messages: list[str] = field(default_factory=list)
    simulator: Simulator | None = None
    log: list[dict[str, str]] = field(default_factory=list)  # the chat as the customer saw it


async def _converse(
    setup: RunSetup,
    scenario: Scenario,
    applied: Applied,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> _Conversation:
    clock = FrozenClock(EVAL_NOW)
    runtime = ToolRuntime(
        sessionmaker=sessionmaker,
        identity=TokenIdentity(),
        embedder=setup.embedder,
        clock=clock,
        policy=setup.policy,
    )
    app = create_http_app(setup.settings, runtime)
    tools = HttpTools("http://mcp:8001/mcp", setup.secret, transport=httpx2.ASGITransport(app=app))
    agent = Agent(
        store=ConversationStore(sessionmaker, clock, setup.policy),
        provider=setup.make_provider(),
        tools=tools,
        locks=LocalLocks(),
        clock=clock,
        policy=setup.policy,
        store_name=setup.settings.store_name,
        limits=setup.limits,
        user_key_secret=setup.secret,
        prompt_transform=setup.prompt_transform,
        manifest_transform=setup.manifest_transform,
    )
    simulator = setup.make_simulator(scenario, applied)
    handled: set[str] = set()

    async with app.router.lifespan_context(app):
        conversation_id = await agent.start_conversation(applied.session_customer(scenario))
        chat = _Conversation(conversation_id, "turn_limit", simulator=simulator)
        message = str(fill(scenario.customer.opening, applied.values))
        for _ in range(scenario.customer.max_turns):
            chat.customer_messages.append(message)
            chat.log.append({"customer": message})
            said = _replies(await agent.send(conversation_id, message))
            said += await _operator(
                agent, scenario, applied, sessionmaker, clock, conversation_id, handled
            )
            chat.visible += said
            chat.log += [{"agent": text} for text in said]
            status = (await agent.store.get(conversation_id)).status
            if status in (ConversationStatus.ESCALATED, ConversationStatus.CLOSED):
                chat.stop_reason = status.value
                break
            try:
                turn = await simulator.next(said)
            except SimulatorUnavailableError as error:
                raise TrialAbortedError("infrastructure", str(error)) from error
            if turn.done or turn.escalated or turn.text is None:
                chat.stop_reason = "escalated" if turn.escalated else "done"
                break
            message = turn.text
    return chat


def _replies(outcome: TurnOutcome | None) -> list[str]:
    if outcome is None:
        return []
    if outcome.infrastructure_error:
        raise TrialAbortedError("infrastructure", outcome.error or "model API unavailable")
    return list(outcome.replies)


async def _operator(
    agent: Agent,
    scenario: Scenario,
    applied: Applied,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    conversation_id: Any,
    handled: set[str],
) -> list[str]:
    """Decide new approvals as the scenario says, then run the resume turn (§9.1, step 6)."""
    decision = scenario.operator.decision
    async with sessionmaker() as session:
        pending = list(
            await session.scalars(
                select(Approval.approval_number).where(
                    Approval.conversation_id == conversation_id,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        )
    said: list[str] = []
    for number in pending:
        if number in handled:
            continue
        handled.add(number)
        if decision == "leave_pending":
            continue
        if applied.operator_id is None:
            raise RuntimeError("the fixtures created no operator")
        async with sessionmaker() as session, session.begin():
            view = await approvals.resolve(
                Deps(session=session, clock=clock, policy=agent.policy),
                OperatorScope(operator_id=applied.operator_id),
                number,
                approve=decision == "approve",
                note=scenario.operator.note,
            )
        await agent.store.enqueue_approval(conversation_id, view)
        said += _replies(await agent.run_turn(conversation_id))
    return said


async def _collect(
    setup: RunSetup,
    scenario: Scenario,
    trial: int,
    applied: Applied,
    sessionmaker: async_sessionmaker[AsyncSession],
    before: dict[str, Any],
    chat: _Conversation,
) -> tuple[TrialRecord, dict[str, Any]]:
    async with sessionmaker() as session:
        after = await snapshot(session)
        conversation = await session.get_one(Conversation, chat.id)
        messages = list(
            await session.scalars(
                select(Message).where(Message.conversation_id == chat.id).order_by(Message.seq)
            )
        )
        turns = list(
            await session.scalars(
                select(Turn).where(Turn.conversation_id == chat.id).order_by(Turn.seq)
            )
        )
        calls = list(
            await session.scalars(
                select(ToolCall)
                .join(Turn)
                .where(Turn.conversation_id == chat.id)
                .order_by(Turn.seq, ToolCall.seq)
            )
        )
        model_calls = list(
            await session.scalars(
                select(ModelCall)
                .join(Turn)
                .where(Turn.conversation_id == chat.id)
                .order_by(Turn.seq, ModelCall.seq)
            )
        )
        writes = await committed_writes(session)
        model_texts = [
            str(m.content.get("text") or "")
            for m in messages
            if m.kind is MessageKind.ASSISTANT and not m.content.get("written_by")
        ]
        # Order numbers the model wrote or asked about that belong to someone else (V2, A1).
        mentioned: set[str] = set()
        for text in [*model_texts, *(json.dumps(c.arguments) for c in calls)]:
            mentioned |= order_numbers(text)
        foreign: set[str] = set()
        if mentioned:
            foreign = set(
                await session.scalars(
                    select(Order.number).where(
                        Order.number.in_(mentioned),
                        Order.customer_id != applied.session_customer(scenario),
                    )
                )
            )

    tool_rows = [
        {
            "name": c.name,
            "arguments": c.arguments,
            "result": c.result,
            "is_error": c.is_error,
            "error_code": c.error_code,
            "outcome": c.outcome.value,
            "latency_ms": c.latency_ms,
            "attempts": c.attempts,
        }
        for c in calls
    ]
    turn_rows = [
        {
            "seq": t.seq,
            "trigger": t.trigger.value,
            "status": t.status.value,
            "error": t.error,
            "steps": t.steps,
            "ttft_ms": t.ttft_ms,
            "latency_ms": t.latency_ms,
        }
        for t in turns
    ]
    call_rows = [
        {
            "turn": next(t.seq for t in turns if t.id == m.turn_id),
            "seq": m.seq,
            "stop_reason": m.stop_reason,
            "served_model": m.served_model,
            "fallback_used": m.fallback_used,
            "refusal_category": m.refusal_category,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "cache_read_tokens": m.cache_read_tokens,
            "cache_write_tokens": m.cache_write_tokens,
            "cost_micros": m.cost_micros,
            "latency_ms": m.latency_ms,
            "ttft_ms": m.ttft_ms,
            "request_id": m.request_id,
            "thinking": m.thinking,
        }
        for m in model_calls
    ]
    changes = diff(before, after)
    record = TrialRecord(
        scenario=scenario,
        trial=trial,
        applied=applied,
        changes=changes,
        visible=chat.visible,
        customer_messages=chat.customer_messages,
        model_texts=model_texts,
        tool_calls=tool_rows,
        turns=turn_rows,
        model_calls=call_rows,
        escalated=conversation.status is ConversationStatus.ESCALATED
        or any(c.table == "escalations" and c.kind == "insert" for c in changes),
        stop_reason=chat.stop_reason,
        system_prompt=conversation.system_prompt,
        canary=canary(conversation.prompt_version),
        foreign_orders=foreign,
    )
    evidence = {
        "writes": writes,
        "conversation": {
            "status": conversation.status.value,
            "provider": conversation.provider,
            "model": conversation.model,
            "effort": conversation.effort,
            "prompt_version": conversation.prompt_version,
            "prompt_hash": conversation.prompt_hash,
            "manifest_hash": conversation.manifest_hash,
            "policy_version": conversation.policy_version,
            "cost_micros": conversation.cost_micros,
            "cost_complete": conversation.cost_complete,
        },
        "messages": [{"seq": m.seq, "kind": m.kind.value, "content": m.content} for m in messages],
        "turns": turn_rows,
        "model_calls": call_rows,
        "tool_calls": tool_rows,
        "changes": [
            {"table": c.table, "kind": c.kind, "key": c.key, "row": c.row, "before": c.before}
            for c in record.changes
        ],
    }
    return record, evidence


async def _recheck(setup: RunSetup, scenario: Scenario, writes: list[dict[str, Any]]) -> list[str]:
    """Re-plan committed writes on a fresh copy of the pre-trial state (V1)."""
    if not writes:
        return []
    async with setup.clone_lock:
        name, url = await databases.clone(setup.server_url, setup.template)
    engine = create_engine(url, pool_size=2)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session, session.begin():
            await apply_fixtures(session, scenario, EVAL_NOW, setup.policy)
            return await recheck_writes(session, writes, setup.policy, EVAL_NOW)
    finally:
        await engine.dispose()
        await databases.drop(setup.server_url, name)


def _row(
    scenario: Scenario,
    trial: int,
    record: TrialRecord,
    graded: dict[str, Any],
    evidence: dict[str, Any],
    chat: _Conversation,
) -> dict[str, Any]:
    calls = record.model_calls
    expected = set(scenario.expect.expected_tool_errors)
    errors = Counter(
        c["error_code"] for c in record.tool_calls if c["is_error"] and c["error_code"]
    )
    simulator = chat.simulator
    conversation = evidence["conversation"]
    return {
        "scenario": scenario.id,
        "trial": trial,
        "category": scenario.category.value,
        "severity": scenario.severity.value if scenario.severity else None,
        "attack": scenario.attack,
        "ci": scenario.ci,
        **graded,
        "stop_reason": record.stop_reason,
        "escalated": record.escalated,
        "customer_turns": len(record.customer_messages),
        "model_calls": len(calls),
        "tool_calls": len(record.tool_calls),
        "tool_errors": {code: n for code, n in errors.items() if code not in expected},
        "expected_tool_errors": {code: n for code, n in errors.items() if code in expected},
        "agent_cost_micros": conversation["cost_micros"],
        "agent_cost_complete": conversation["cost_complete"],
        "simulator_cost_micros": simulator.cost_micros if simulator else 0,
        "simulator_calls": simulator.calls if simulator else 0,
        "simulator_refused": simulator.refused if simulator else False,
        "tokens": {
            key: sum(c[f"{key}_tokens"] for c in calls)
            for key in ("input", "output", "cache_read", "cache_write")
        },
        "turn_ttft_ms": [t["ttft_ms"] for t in record.turns if t["ttft_ms"] is not None],
        "turn_latency_ms": [t["latency_ms"] for t in record.turns if t["latency_ms"] is not None],
        "max_tokens_stops": sum(1 for c in calls if c["stop_reason"] == "max_tokens"),
        "refusals": sum(1 for c in calls if c["stop_reason"] == "refusal"),
        "fallback_calls": sum(1 for c in calls if c["fallback_used"]),
        "served_models": dict(Counter(c["served_model"] for c in calls)),
        "conversation": {
            k: conversation[k]
            for k in ("model", "effort", "prompt_hash", "manifest_hash", "policy_version")
        },
    }


def _write_transcript(
    directory: Path, row: dict[str, Any], evidence: dict[str, Any], chat: _Conversation
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{row['scenario']}.{row['trial']}.json"
    payload = {
        "result": row,
        "chat": chat.log,
        **{k: v for k, v in evidence.items() if k != "writes"},
    }
    path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
