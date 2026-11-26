"""Whole trials on real databases with a scripted agent: the driver, the operator stub, the
graders, the runner and the self-tests, without a model."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from tests.support.factories import server_url

from relay.agent.providers.scripted import Call, Reply, ScriptedProvider, ScriptedRequest
from relay.config import Settings
from relay.db.models import Customer
from relay.db.session import create_engine, make_sessionmaker
from relay.domain.enums import ReturnReason, WriteAction
from relay.domain.policy_config import default_policy
from relay.domain.returns import ReturnArgs
from relay.evals import databases, harness, runner
from relay.evals.databases import EVAL_NOW, Template
from relay.evals.fixtures import Applied, apply_fixtures
from relay.evals.grading import recheck_writes
from relay.evals.scenario import Scenario, fill, load_scenarios
from relay.evals.selftest import NullProvider, offline_setup, run_selftest
from relay.evals.simulator import ScriptedSimulator, Simulator
from relay.evals.trial import RunSetup, run_trial
from relay.kb.embeddings import HashingEmbedder

ROOT = Path(__file__).resolve().parents[4] / "evals"
SCENARIOS = {s.id: s for s in load_scenarios([ROOT / "scenarios", ROOT / "redteam"])}
VALUES = {"o1": {"number": "PV-900001"}}


def setup_for(
    settings: Settings,
    template: Template,
    steps: list[Any],
    customer_says: list[str],
    tmp_path: Path | None = None,
) -> RunSetup:
    def simulator(scenario: Scenario, applied: Applied) -> Simulator:
        return ScriptedSimulator([fill(m, applied.values) for m in customer_says])

    return RunSetup(
        settings=harness.with_secret(settings),
        server_url=server_url(),
        template=template.name,
        policy=default_policy(),
        embedder=HashingEmbedder(),
        make_provider=lambda: ScriptedProvider(list(steps)),
        make_simulator=simulator,
        transcripts=tmp_path,
        wall_clock_s=120,
    )


async def test_a_good_scripted_agent_passes_a_refund_trial(
    eval_settings: Settings, light_template: Template, tmp_path: Path
) -> None:
    scenario = SCENARIOS["happy-refund-damaged-under-limit"]
    refund = {
        "order_number": "PV-900001",
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }

    def commit(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    steps = [
        Reply(calls=(Call("issue_refund", refund),)),
        Reply(text="I can refund $59.99 to your Visa ending 4242. Shall I go ahead?"),
        commit,
        Reply(text="Done: $59.99 is on its way to your Visa."),
    ]
    setup = setup_for(eval_settings, light_template, steps, ["Yes, please."], tmp_path)

    row = await run_trial(setup, scenario, 1)

    assert row["success"], row["checks"]
    assert row["checks"]["db"] == {"ok": True, "missing": [], "unexpected": []}
    assert (row["customer_turns"], row["model_calls"], row["tool_calls"]) == (2, 4, 2)
    assert row["stop_reason"] == "done"
    assert row["violations"] == []
    assert row["attempts"] == []
    assert row["clone_ms"] > 0
    transcript = json.loads((tmp_path / "happy-refund-damaged-under-limit.1.json").read_text())
    assert transcript["chat"][0] == {"customer": fill(scenario.customer.opening, VALUES)}
    assert [c["table"] for c in transcript["changes"]] == ["refunds"]


async def test_a_leaking_agent_is_caught_even_when_the_guard_retracts(
    eval_settings: Settings, light_template: Template
) -> None:
    def leak(sent: ScriptedRequest) -> Reply:
        return Reply(text=" ".join(sent.request.system.split()[:60]))

    setup = setup_for(eval_settings, light_template, [leak], [])

    row = await run_trial(setup, SCENARIOS["rt-prompt-extraction"], 1)

    assert not row["success"]
    assert [v["id"] for v in row["violations"]] == ["V3"]


async def test_the_operator_stub_approves_and_the_resume_turn_runs(
    eval_settings: Settings, light_template: Template
) -> None:
    scenario = SCENARIOS["hitl-refund-approved"]
    refund = {
        "order_number": "PV-900001",
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }

    def commit(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    def resumed(sent: ScriptedRequest) -> Reply:
        assert "approved it and it has been carried out" in sent.messages[-1]["content"]
        return Reply(text="Good news: your $349.99 refund was approved.")

    steps = [
        Reply(calls=(Call("issue_refund", refund),)),
        Reply(text="$349.99 needs a quick review. Go ahead?"),
        commit,
        Reply(text="Submitted for review."),
        resumed,
    ]
    row = await run_trial(setup_for(eval_settings, light_template, steps, ["Yes."]), scenario, 1)

    assert row["success"], row["checks"]


async def test_the_policy_recheck_flags_a_write_the_policy_denies(light_template: Template) -> None:
    scenario = SCENARIOS["edge-return-day-31"]
    name, url = await databases.clone(server_url(), light_template.name)
    engine = create_engine(url, pool_size=2)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            applied = await apply_fixtures(session, scenario, EVAL_NOW, default_policy())
            email = await session.scalar(
                select(Customer.email).where(Customer.id == applied.session_customer(scenario))
            )
            # The canonical form the server stores on a preview: what a real trial re-checks.
            args = ReturnArgs(
                order_number="PV-900001", lines={1: 1}, reason=ReturnReason.CHANGED_MIND
            )
            write = {
                "action": WriteAction.CREATE_RETURN,
                "args": args.canonical(),
                "customer": email,
            }
            denials = await recheck_writes(session, [write], default_policy(), EVAL_NOW)
    finally:
        await engine.dispose()
        await databases.drop(server_url(), name)

    assert len(denials) == 1
    assert denials[0].startswith("create_return on PV-900001: policy ")


async def test_a_run_resumes_where_it_stopped(
    eval_settings: Settings, light_template: Template, tmp_path: Path
) -> None:
    scenario = SCENARIOS["happy-policy-question"]
    setup = offline_setup(
        harness.with_secret(eval_settings),
        light_template,
        server_url(),
        default_policy(),
        HashingEmbedder(),
        NullProvider,
    )
    config = harness.config_block(
        run_id="resume-test",
        command="test",
        settings=eval_settings,
        policy=default_policy(),
        scenarios=[scenario],
        selection="test",
        k=2,
        concurrency=2,
        template=light_template,
        manifest="m",
        ablation=None,
    )
    (tmp_path / "config.json").write_text(json.dumps(config))
    ran: list[tuple[str, int]] = []

    async def counted(setup: RunSetup, scenario: Scenario, trial: int) -> dict[str, Any]:
        ran.append((scenario.id, trial))
        return await run_trial(setup, scenario, trial)

    await runner.execute(setup, [scenario], k=2, concurrency=2, run_dir=tmp_path, trial_fn=counted)
    results = tmp_path / "results.jsonl"
    kept = [line for line in results.read_text().splitlines() if '"trial": 1' in line]
    results.write_text("\n".join(kept) + "\n")
    report = await runner.execute(
        setup, [scenario], k=2, concurrency=2, run_dir=tmp_path, trial_fn=counted
    )

    assert sorted(ran) == [(scenario.id, 1), (scenario.id, 2), (scenario.id, 2)]
    assert len(report["rows"]) == 2
    assert (tmp_path / "report.md").read_text().startswith("# Eval run resume-test")


async def test_the_self_tests_pass_on_a_sample(
    eval_settings: Settings, light_template: Template
) -> None:
    chosen = [
        SCENARIOS["happy-refund-damaged-under-limit"],
        SCENARIOS["rt-order-note-injection"],
        SCENARIOS["escalation-asks-for-human"],
    ]
    outcomes = await run_selftest(
        harness.with_secret(eval_settings),
        chosen,
        light_template,
        server_url(),
        default_policy(),
        HashingEmbedder(),
    )
    failed = [o for o in outcomes if not o.ok]
    assert failed == []
    assert {o.check for o in outcomes} == {
        "policy agreement",
        "oracle",
        "null agent",
        "recompute",
        "timing",
        "infrastructure error",
    }
