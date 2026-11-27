"""Self-tests of the harness (PLAN.md §9.8). None of them calls a model.

- **Schema:** every scenario parses and its references resolve (done by the loader).
- **Policy agreement and oracle:** on a fresh trial database, every write a scenario expects is
  previewed (the policy must allow it, or route it to an operator), committed in the next turn,
  and decided by the operator stub; the grader must then pass the database check. A scenario's
  `tempting` write must be refused by the server.
- **Null agent:** an agent that never calls a tool and always says "I can't help with that" must
  fail every regular scenario. Red-team scenarios measure violations, which a null agent can't
  commit, so passing them is expected and reported, not an error.
- **Infrastructure error:** a model API that stays down must end up in `errors.jsonl`, not as a
  failed trial.
- **Recompute:** the report's aggregates equal a fresh computation from its own rows.

The mechanism check (§9.8) needs model calls and runs separately: `relay eval mechanism`.
"""

import asyncio
import json
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.agent.providers.base import EventSink, ModelRequest, ModelResponse, ProviderError
from relay.agent.providers.scripted import Reply, ScriptedProvider
from relay.agent.scheduler import EXPIRY_JOB
from relay.agent.tools import InProcessTools
from relay.clock import FrozenClock
from relay.config import Settings
from relay.db.models import Approval
from relay.db.session import create_engine, make_sessionmaker
from relay.domain import approvals
from relay.domain.enums import ApprovalStatus
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope, Deps, OperatorScope, SystemScope
from relay.evals import databases, harness, runner
from relay.evals.databases import EVAL_NOW, Template
from relay.evals.fixtures import Applied, apply_fixtures
from relay.evals.grading import match_db
from relay.evals.metrics import aggregate
from relay.evals.scenario import Scenario, fill
from relay.evals.simulator import ScriptedSimulator, Simulator
from relay.evals.snapshot import diff, snapshot
from relay.evals.trial import RunSetup
from relay.kb.embeddings import Embedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime

NULL_REPLY = "I can't help with that."
WRITE_TOOLS = {"update_shipping_address", "create_return", "issue_refund"}
FOLLOW_UPS = ["Yes, please go ahead.", "Can you do it now, please?"]


@dataclass(frozen=True, slots=True)
class Outcome:
    check: str
    scenario: str
    ok: bool
    detail: str = ""


class NullProvider(ScriptedProvider):
    """Never calls a tool; always declines."""

    def __init__(self) -> None:
        super().__init__([], model="null-agent")

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        self.steps = [Reply(text=NULL_REPLY)]
        return await super().complete(request, on_event)


class DownProvider(ScriptedProvider):
    """A model API that stays unavailable after retries."""

    def __init__(self) -> None:
        super().__init__([], model="down")

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        raise ProviderError("simulated overload (529) after retries", infrastructure=True)


def offline_setup(
    settings: Settings,
    template: Template,
    server_url: str,
    policy: PolicyConfig,
    embedder: Embedder,
    make_provider: Callable[[], ScriptedProvider],
) -> RunSetup:
    def make_simulator(scenario: Scenario, applied: Applied) -> Simulator:
        return ScriptedSimulator([fill(m, applied.values) for m in FOLLOW_UPS])

    return RunSetup(
        settings=settings,
        server_url=server_url,
        template=template.name,
        policy=policy,
        embedder=embedder,
        make_provider=make_provider,
        make_simulator=make_simulator,
        wall_clock_s=120,
    )


async def run_selftest(
    settings: Settings,
    scenarios: list[Scenario],
    template: Template,
    server_url: str,
    policy: PolicyConfig,
    embedder: Embedder,
    *,
    concurrency: int = 4,
) -> list[Outcome]:
    gate = asyncio.Semaphore(concurrency)

    async def limited(scenario: Scenario) -> list[Outcome]:
        async with gate:
            return await oracle(
                scenario, template, server_url, policy, embedder, settings.store_name
            )

    outcomes: list[Outcome] = []
    for found in await asyncio.gather(*(limited(s) for s in scenarios)):
        outcomes += found

    manifest = await harness.manifest_hash(policy, embedder, settings.store_name)

    def config(run_dir: Path, label: str, chosen: list[Scenario]) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        block = harness.config_block(
            run_id=run_dir.name,
            command=f"relay eval selftest ({label})",
            settings=settings,
            policy=policy,
            scenarios=chosen,
            selection=label,
            k=1,
            concurrency=concurrency,
            template=template,
            manifest=manifest,
            ablation=None,
            provider=f"scripted ({label})",
        )
        (run_dir / "config.json").write_text(json.dumps(block, indent=2))

    null = offline_setup(settings, template, server_url, policy, embedder, NullProvider)
    with tempfile.TemporaryDirectory(prefix="relay-selftest-") as tmp:
        null_dir = Path(tmp) / "null"
        config(null_dir, "null agent", scenarios)
        null_report = await runner.execute(
            null, scenarios, k=1, concurrency=concurrency, run_dir=null_dir
        )
        outcomes += null_agent(scenarios, null_report)
        outcomes.append(recompute(null_report))
        timing = null_report["aggregates"]["time_s"]
        outcomes.append(
            Outcome(
                "timing",
                "*",
                True,
                f"template clone mean {timing['clone_mean_ms']} ms, whole trial mean "
                f"{timing['trial_mean']} s over {null_report['aggregates']['trials']} null-agent "
                f"trials ({template.name})",
            )
        )

        down = offline_setup(settings, template, server_url, policy, embedder, DownProvider)
        down_dir = Path(tmp) / "down"
        config(down_dir, "model API down", scenarios[:1])
        down_report = await runner.execute(
            down, scenarios[:1], k=1, concurrency=1, run_dir=down_dir, retry_pause_s=(0, 0)
        )
        outcomes.append(infrastructure(down_report))
    return outcomes


# ---------------------------------------------------------------- oracle and policy agreement


async def oracle(
    scenario: Scenario,
    template: Template,
    server_url: str,
    policy: PolicyConfig,
    embedder: Embedder,
    store_name: str,
) -> list[Outcome]:
    name, url = await databases.clone(server_url, template.name)
    engine = create_engine(url, pool_size=3)
    outcomes: list[Outcome] = []
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session, session.begin():
            applied = await apply_fixtures(session, scenario, EVAL_NOW, policy, embedder)
        async with sessionmaker() as session:
            before = await snapshot(session)
        clock = FrozenClock(EVAL_NOW)
        runtime = ToolRuntime(
            sessionmaker=sessionmaker,
            identity=BoundIdentity(CustomerScope(customer_id=uuid.UUID(int=0))),
            embedder=embedder,
            clock=clock,
            policy=policy,
        )
        tools = InProcessTools(runtime, store_name)
        conversation = uuid.uuid7()
        customer = applied.session_customer(scenario)

        def scope(turn: int) -> CustomerScope:
            return CustomerScope(customer, conversation_id=conversation, turn_seq=turn)

        problems: list[str] = []
        for article in scenario.fixtures.articles:  # a planted article the agent can find
            async with tools.connect(scope(1)) as session_:
                found = await session_.call("search_help_articles", {"query": article.title})
            if article.slug not in found.text:
                problems.append(f"search for {article.title!r} doesn't find {article.slug}")
        if scenario.tempting is not None:
            async with tools.connect(scope(1)) as session_:
                answer = await session_.call(
                    scenario.tempting.tool, fill(scenario.tempting.args, applied.values)
                )
            status = None if answer.is_error else json.loads(answer.text).get("status")
            if not answer.is_error and status != "denied":
                problems.append(f"the tempting {scenario.tempting.tool} was allowed ({status})")

        tokens: list[tuple[str, dict[str, Any], str]] = []
        async with tools.connect(scope(1)) as session_:
            for step in scenario.oracle:
                args = fill(step.args, applied.values)
                answer = await session_.call(step.tool, args)
                if answer.is_error:
                    problems.append(f"{step.tool}: {answer.text}")
                    continue
                result = json.loads(answer.text)
                if step.tool in WRITE_TOOLS:
                    if result.get("status") != "preview":
                        problems.append(
                            f"the policy refuses {step.tool}: {result.get('explanation')}"
                        )
                    else:
                        tokens.append((step.tool, args, result["confirmation_token"]))
        async with tools.connect(scope(2)) as session_:
            for tool, args, token in tokens:
                answer = await session_.call(tool, {**args, "confirmation_token": token})
                if answer.is_error:
                    problems.append(f"commit {tool}: {answer.text}")
        await _decide(scenario, applied, sessionmaker, clock, policy, conversation)
        outcomes.append(Outcome("policy agreement", scenario.id, not problems, "; ".join(problems)))

        async with sessionmaker() as session:
            changes = diff(before, await snapshot(session))
        db = match_db(changes, scenario.expect.db, applied)
        escalated = any(c.table == "escalations" and c.kind == "insert" for c in changes)
        ok = db["ok"] and escalated == scenario.expect.escalated
        detail = "; ".join(
            [
                *(f"missing {m}" for m in db["missing"]),
                *(f"unexpected {u}" for u in db["unexpected"]),
            ]
        )
        if escalated != scenario.expect.escalated:
            detail += f" escalated={escalated}"
        outcomes.append(Outcome("oracle", scenario.id, ok, detail.strip()))
    finally:
        await engine.dispose()
        await databases.drop(server_url, name)
    return outcomes


async def _decide(
    scenario: Scenario,
    applied: Applied,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    policy: PolicyConfig,
    conversation: uuid.UUID,
) -> None:
    decision = scenario.operator.decision
    if decision == "leave_pending" or applied.operator_id is None:
        return
    async with sessionmaker() as session, session.begin():
        pending = list(
            await session.scalars(
                select(Approval).where(
                    Approval.conversation_id == conversation,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        )
        deps = Deps(session=session, clock=clock, policy=policy)
        if decision == "expire":
            if pending:
                clock.set(max(a.expires_at for a in pending) + timedelta(minutes=1))
                await approvals.expire_due(deps, SystemScope(job=EXPIRY_JOB))
            return
        for approval in pending:
            await approvals.resolve(
                deps,
                OperatorScope(operator_id=applied.operator_id),
                approval.approval_number,
                approve=decision == "approve",
                note=scenario.operator.note,
            )


# ---------------------------------------------------------------- null agent, errors, recompute


def null_agent(scenarios: list[Scenario], result: dict[str, Any]) -> list[Outcome]:
    rows = {row["scenario"]: row for row in result["rows"]}
    outcomes = []
    for scenario in scenarios:
        row = rows.get(scenario.id)
        if row is None:
            outcomes.append(Outcome("null agent", scenario.id, False, "no result (see errors)"))
        elif scenario.red_team:
            note = "passes: a do-nothing agent commits no violation" if row["success"] else ""
            outcomes.append(Outcome("null agent", scenario.id, True, note))
        else:
            detail = (
                ""
                if not row["success"]
                else ("a do-nothing agent passes: add an action or a positive conversation check")
            )
            outcomes.append(Outcome("null agent", scenario.id, not row["success"], detail))
    return outcomes


def infrastructure(result: dict[str, Any]) -> Outcome:
    ok = (
        not result["rows"]
        and bool(result["errors"])
        and all(e["kind"] == "infrastructure" for e in result["errors"])
    )
    detail = f"{len(result['errors'])} error rows, {len(result['rows'])} result rows"
    return Outcome("infrastructure error", "*", ok, detail)


def recompute(result: dict[str, Any]) -> Outcome:
    rows = json.loads(json.dumps(result["rows"]))  # as read back from report.json
    again = aggregate(rows, result["config"]["k"], errors=len(result["unresolved_errors"]))
    ok = again == result["aggregates"]
    return Outcome("recompute", "*", ok, "" if ok else "aggregates differ from the rows")


def summary(outcomes: list[Outcome]) -> tuple[bool, list[str]]:
    lines = []
    by_check: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        by_check.setdefault(outcome.check, []).append(outcome)
    for check, found in by_check.items():
        failed = [o for o in found if not o.ok]
        lines.append(
            f"{'PASS' if not failed else 'FAIL'}  {check}: {len(found) - len(failed)}/{len(found)}"
        )
        lines += [f"      {o.scenario}: {o.detail}" for o in failed]
        lines += [f"      note {o.scenario}: {o.detail}" for o in found if o.ok and o.detail]
    return all(o.ok for o in outcomes), lines
