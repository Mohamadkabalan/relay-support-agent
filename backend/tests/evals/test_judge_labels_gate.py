"""Phase 7's eval pieces that need nothing running: the judge's parsing and claims check, the
labels and agreement, the CI gate's rule, and publishing between markers."""

import json
import shutil
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from tests.evals.test_harness_units import ROOT, harness_config, row, scenarios
from tests.support.fake_apis import Recorder, claude_message
from typer.testing import CliRunner

from relay.agent.prompts import system_prompt
from relay.cli import app
from relay.domain.policy_config import default_policy
from relay.evals import gate, labels, publish
from relay.evals.judge import (
    CLAIM_KINDS,
    CRITERIA,
    OUTPUT_SCHEMA,
    Judge,
    JudgeError,
    material,
    parse,
    policy_summary,
    unbacked_claims,
)
from relay.evals.metrics import aggregate
from relay.evals.report import markdown

# ---------------------------------------------------------------- the judge


def verdict_json(failing: tuple[str, ...] = (), claims: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "criteria": {k: {"reason": "r", "pass": k not in failing} for k in CRITERIA},
            "claims": claims or [],
        }
    )


def test_the_output_schema_is_strict_everywhere() -> None:
    """Structured outputs need every object closed and every property required."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(OUTPUT_SCHEMA)
    assert list(OUTPUT_SCHEMA["properties"]["criteria"]["properties"]) == list(CRITERIA)


def test_parse_keeps_known_criteria_and_claim_kinds() -> None:
    text = verdict_json(
        failing=("tone",),
        claims=[{"kind": "refund_issued", "quote": "Done!"}, {"kind": "magic", "quote": "x"}],
    )
    verdict = parse(text, "claude-sonnet-5-5", 1234)
    assert not verdict.all_pass
    assert verdict.criteria["tone"] == {"pass": False, "reason": "r"}
    assert verdict.claims == [{"kind": "refund_issued", "quote": "Done!"}]
    assert verdict.to_json()["cost_micros"] == 1234
    with pytest.raises(JudgeError, match="unusable"):
        parse('{"criteria": {}}', "m", None)
    with pytest.raises(JudgeError):
        parse("not json", "m", None)


def test_claims_are_checked_against_the_database_changes() -> None:
    changes: list[dict[str, Any]] = [
        {"table": "refunds", "kind": "insert", "row": {}, "before": None},
        {
            "table": "orders",
            "kind": "update",
            "row": {"shipping_address": {"line1": "New"}, "status": "processing"},
            "before": {"shipping_address": {"line1": "Old"}, "status": "processing"},
        },
    ]
    claims = [{"kind": kind, "quote": kind} for kind in CLAIM_KINDS]
    assert [c["kind"] for c in unbacked_claims(claims, changes)] == [
        "return_created",
        "approval_submitted",
        "escalated",
    ]
    status_only: list[dict[str, Any]] = [
        {"table": "orders", "kind": "update", "row": {"status": "x"}, "before": {"status": "y"}}
    ]
    assert unbacked_claims([{"kind": "address_changed", "quote": "q"}], status_only) == [
        {"kind": "address_changed", "quote": "q"}
    ]


def test_the_material_shows_the_goal_policies_and_every_step() -> None:
    summary = policy_summary(system_prompt(default_policy(), "Pixel & Volt").text)
    assert summary.startswith("The tools enforce these rules")
    assert "RLY-" not in summary
    assert "Confidentiality" not in summary
    assert policy_summary("no such section") == ""
    text = material(
        goal="Get a refund.",
        policy_summary=summary,
        messages=[
            {"kind": "customer", "content": {"text": "Refund please"}},
            {
                "kind": "assistant",
                "content": {
                    "text": "Checking.",
                    "tool_calls": [{"name": "get_order", "arguments": {"order_number": "PV-1"}}],
                },
            },
            {
                "kind": "tool_results",
                "content": {"results": [{"name": "get_order", "content": "{}"}]},
            },
            {"kind": "system_note", "content": {"text": "approved"}},
            {"kind": "notice", "content": {"text": "Handed over."}},
            {"kind": "assistant", "content": {"text": "Sorry.", "written_by": "runtime"}},
            {"kind": "event", "content": {"text": "(not shown)"}},
        ],
    )
    assert "<customer_goal>\nGet a refund.\n</customer_goal>" in text
    assert 'AGENT CALLS get_order {"order_number": "PV-1"}' in text
    assert "TOOL RESULT get_order: {}" in text
    assert "TRUSTED NOTE TO THE AGENT: approved" in text
    assert "SHOWN TO THE CUSTOMER: Handed over." in text
    assert "AGENT (fixed text from the runtime): Sorry." in text
    assert "(not shown)" not in text


async def test_the_judge_request_and_its_failures() -> None:
    def judged(body: dict[str, Any]) -> httpx2.Response:
        message = claude_message(
            [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "text", "text": verdict_json()},
            ],
            model="claude-sonnet-5-5",
        )
        return httpx2.Response(200, json=message)

    recorder = Recorder(
        [
            judged,
            httpx2.Response(200, json=claude_message([], stop_reason="max_tokens")),
            httpx2.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad"},
                },
            ),
        ]
    )
    client = AsyncAnthropic(api_key="k", http_client=recorder.client(), max_retries=0)
    judge = Judge(client, model="claude-sonnet-5-5", store_name="Pixel & Volt", tools=["get_order"])

    verdict = await judge.score("the material")
    assert verdict.all_pass
    body = recorder.bodies()[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        "effort": "medium",
    }
    assert "get_order" in body["system"][0]["text"]
    assert body["messages"] == [{"role": "user", "content": "the material"}]
    with pytest.raises(JudgeError, match="max_tokens"):
        await judge.score("again")
    with pytest.raises(JudgeError, match="API error"):
        await judge.score("again")


def judged_row(
    scenario: str, trial: int, success: bool, failing: tuple[str, ...] = ()
) -> dict[str, Any]:
    verdict = parse(verdict_json(failing), "claude-sonnet-5-5", 20_000).to_json()
    return row(
        scenario, trial, success, judge={**verdict, "unbacked_claims": []}, judge_cost_micros=20_000
    )


def test_judge_aggregates_and_the_report_section() -> None:
    rows = [
        judged_row("a", 1, True),
        judged_row("a", 2, True, failing=("tone",)),
        row("b", 1, False, judge={"error": "judge stopped with refusal"}),
        row("b", 2, True),
    ]
    agg = aggregate(rows, k=2)
    judge = agg["judge"]
    assert (judge["judged"], judge["errors"]) == (2, 1)
    assert judge["all_pass"]["successes"] == 1
    assert judge["criteria"]["tone"]["successes"] == 1
    assert judge["cost_total"] == 0.04
    assert agg["cost_usd"]["judge_total"] == 0.04
    config = {
        **harness_config(),
        "judge_model": "claude-sonnet-5-5",
        "judge_effort": "medium",
        "rubric": "r1",
    }
    report = {
        "config": config,
        "aggregates": agg,
        "rows": rows,
        "errors": [],
        "unresolved_errors": [],
        "triage": {"b.1": {"cause": "prompt gap", "note": "asked twice"}},
    }
    text = markdown(report)
    assert "| Judge: trials passing all criteria | 1/2 = 50.0%" in text
    assert "1 judge errors" in text
    assert "| tone | 1/2 = 50.0%" in text
    assert "`claude-sonnet-5-5`, effort medium, rubric r1" in text
    assert "| b.1 | missing refunds {} | prompt gap: asked twice |" in text
    assert "Root causes: prompt gap 1." in text


# ---------------------------------------------------------------- labels and agreement


def write_transcripts(run: Path) -> None:
    directory = run / "transcripts"
    directory.mkdir(parents=True)
    for index in range(12):
        category = ["happy_path", "red_team", "hitl"][index % 3]
        success = index % 2 == 0
        result = judged_row(f"s{index}", 1, success)
        result["category"] = category
        payload = {"result": result, "material": f"material {index}"}
        (directory / f"s{index}.1.json").write_text(json.dumps(payload))
    unjudged = {"result": row("x", 1, True), "material": "m"}
    (directory / "x.1.json").write_text(json.dumps(unjudged))


def test_the_sample_is_stratified_and_reproducible(tmp_path: Path) -> None:
    write_transcripts(tmp_path)
    first = labels.sample(tmp_path, size=6)
    assert [item["key"] for item in first] == [
        item["key"] for item in labels.sample(tmp_path, size=6)
    ]
    strata = {(item["category"], item["success"]) for item in first}
    assert len(strata) == 6  # one from each (category, outcome) stratum before any repeats
    assert all(item["key"] != "x.1" for item in labels.sample(tmp_path, size=50))
    assert len(labels.sample(tmp_path, size=50)) == 12
    with pytest.raises(ValueError, match="no judged transcripts"):
        labels.write_sample(tmp_path / "empty", tmp_path / "out")


def test_labeling_is_blind_resumable_and_feeds_agreement(tmp_path: Path) -> None:
    write_transcripts(tmp_path)
    out = tmp_path / "labels"
    labels.write_sample(tmp_path, out, size=3)
    shown: list[str] = []
    answers = iter(["x", "p", *(["p"] * 6), "q"])
    assert labels.label(out, ask=lambda _: next(answers), show=shown.append) == 1
    assert not any("pass" in line and "reason" in line for line in shown)  # no judge verdicts
    assert "material" in "".join(shown)

    remaining = iter(["f", *["p"] * 6, "u", *["p"] * 6])
    assert labels.label(out, ask=lambda _: next(remaining), show=lambda _: None) == 2
    saved = json.loads((out / "labels.json").read_text())
    assert len(saved) == 3

    sample = json.loads((out / "sample.json").read_text())
    first, second, third = (item["key"] for item in sample["items"])
    rejudge = {
        key: parse(verdict_json(failing=("tone",) if key == first else ()), "m", None).to_json()
        for key in (first, second, third)
    }
    (out / "rejudge.json").write_text(json.dumps(rejudge))
    result = labels.agreement(out)
    first_criterion = result["criteria"]["addresses_requests"]
    # The human said pass, fail, unsure; the judge said pass on all three.
    assert first_criterion["clear_cut"] == 2
    assert first_criterion["unsure"] == 1
    assert first_criterion["accuracy"] == 0.5
    assert first_criterion["confusion"]["human_fail_judge_pass"] == 1
    assert not first_criterion["meets_threshold"]
    assert result["criteria"]["tone"]["self_agreement"] == round(2 / 3, 4)
    assert not result["all_meet_threshold"]
    text = labels.markdown(result)
    assert "| addresses_requests | 2 | 1 | 0.50 (below target) |" in text


def test_cohen_kappa() -> None:
    assert labels.cohen_kappa([]) is None
    assert labels.cohen_kappa([(True, True), (True, True)]) is None  # all chance agreement
    assert labels.cohen_kappa([(True, True), (False, False)]) == 1.0
    assert labels.cohen_kappa([(True, False), (False, True)]) == -1.0
    # 20 items: 15 agree; both raters say yes 50% of the time; expected agreement 0.5.
    pairs = [(True, True)] * 8 + [(False, False)] * 7 + [(True, False)] * 2 + [(False, True)] * 3
    assert labels.cohen_kappa(pairs) == 0.5


def test_known_negatives_name_real_criteria() -> None:
    assert len(labels.KNOWN_NEGATIVES) == 5
    for _name, criterion, goal, messages in labels.KNOWN_NEGATIVES:
        assert criterion in CRITERIA
        assert goal
        assert [m["kind"] for m in messages] == ["customer", "assistant"]


# ---------------------------------------------------------------- the CI gate


def scenario_ids() -> dict[str, Any]:
    return {s.id: s for s in scenarios()}


def test_the_gate_rule() -> None:
    known = scenario_ids()
    chosen = [
        known[i]
        for i in (
            "happy-track-package",
            "happy-policy-question",
            "happy-return-in-window",
            "rt-prompt-extraction",
            "happy-lost-package-refund",
        )
    ]
    critical = {"id": "V3", "severity": "critical", "detail": "the canary string"}
    rows = [
        row("happy-track-package", 1, True),
        row("happy-policy-question", 1, False),
        row("happy-return-in-window", 1, False),
        row("rt-prompt-extraction", 1, False, violations=[critical]),
    ]
    first = gate.decide(rows, chosen)
    assert first.outcomes == {
        "happy-track-package": "pass",
        "happy-policy-question": "retrying",
        "happy-return-in-window": "retrying",
        "rt-prompt-extraction": "critical violation",
        "happy-lost-package-refund": "no result",
    }
    assert first.retry == ["happy-policy-question", "happy-return-in-window"]
    assert not first.ok

    rows += [
        row("happy-policy-question", 2, True),
        row("happy-policy-question", 3, True),
        row("happy-return-in-window", 2, False),
        row("happy-return-in-window", 3, True),
    ]
    second = gate.decide(rows, chosen[:3])
    assert second.outcomes == {
        "happy-track-package": "pass",
        "happy-policy-question": "flaky pass",
        "happy-return-in-window": "regression",
    }
    assert second.reasons == ["happy-return-in-window: failed 2 of 3 attempts"]
    assert gate.decide(
        rows[:1] + rows[4:6] + [row("happy-policy-question", 1, False)], chosen[:2]
    ).ok


def full_report() -> dict[str, Any]:
    rows = []
    for scenario in scenarios():
        for trial in (1, 2, 3):
            success = not (scenario.id == "happy-return-in-window" and trial == 2)
            extra = {
                "category": scenario.category.value,
                "severity": scenario.severity.value if scenario.severity else None,
            }
            rows.append(row(scenario.id, trial, success, **extra))
    config = {
        **harness_config(),
        "k": 3,
        "run_id": "20261201T000000Z-abc1234",
        "command": "relay eval run --subset all --k 3 --concurrency 8",
    }
    return {
        "config": config,
        "aggregates": aggregate(rows, 3),
        "rows": rows,
        "errors": [],
        "unresolved_errors": [],
        "triage": {},
    }


def test_the_baseline_and_the_ci_subset_check() -> None:
    report = full_report()
    baseline = gate.build_baseline(report)
    assert baseline["run_id"] == "20261201T000000Z-abc1234"
    assert baseline["scenarios"]["happy-return-in-window"] == {
        "category": "happy_path",
        "severity": None,
        "passed": 2,
        "trials": 3,
    }
    problems = gate.check_ci_subset(baseline, scenarios())
    assert problems == [
        "happy-return-in-window: passed 2/3 in the baseline; the CI subset needs 3/3"
    ]
    assert "happy-return-in-window" not in gate.stable_candidates(baseline, scenarios())
    text = gate.summary(
        gate.decide(report["rows"][:3], scenarios()[:1]), report["rows"][:3], baseline, report
    )
    assert "## Eval gate passed" in text
    assert "| change-of-mind-cancel-before-confirm | 3/3 | pass pass pass | pass |" in text


# ---------------------------------------------------------------- publishing


def test_blocks_are_replaced_between_markers(tmp_path: Path) -> None:
    text = "intro\n<!-- relay:results:start -->\nold\n<!-- relay:results:end -->\nend\n"
    replaced = publish.replace_block(text, "results", "new $1.00 \\1")
    assert (
        replaced
        == "intro\n<!-- relay:results:start -->\nnew $1.00 \\1\n<!-- relay:results:end -->\nend\n"
    )
    with pytest.raises(publish.MarkerError):
        publish.replace_block(text, "judge", "x")
    path = tmp_path / "doc.md"
    path.write_text(text)
    assert publish.rewrite(path, {"results": "a", "judge": "b"}) == ["results"]


def test_published_blocks_name_their_run_and_command() -> None:
    report = full_report()
    blocks = publish.run_blocks(report)
    for name in ("results", "categories", "judge", "triage"):
        assert "20261201T000000Z-abc1234" in blocks[name], name
    assert "relay eval run --subset all --k 3" in blocks["results"]
    assert "| Task success (pass^1) | 239/240" in blocks["results"]
    comparison = publish.comparison_block([report, report])
    assert comparison.count("| `20261201T000000Z-abc1234` |") == 2


def test_the_publish_compare_and_baseline_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evals = tmp_path / "evals"
    shutil.copytree(
        ROOT / "scenarios", evals / "scenarios", ignore=shutil.ignore_patterns("drafts")
    )
    shutil.copytree(ROOT / "redteam", evals / "redteam")
    run = evals / "reports" / "20261201T000000Z-abc1234"
    run.mkdir(parents=True)
    report = full_report()
    (run / "config.json").write_text(json.dumps(report["config"]))
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in report["rows"]))
    (tmp_path / "docs").mkdir()
    markers = "<!-- relay:{0}:start -->\n<!-- relay:{0}:end -->\n"
    (tmp_path / "README.md").write_text(markers.format("results"))
    (tmp_path / "docs" / "EVALS.md").write_text(
        "".join(markers.format(n) for n in ("results", "comparison", "triage"))
    )
    monkeypatch.setenv("RELAY_EVALS_DIR", str(evals))
    runner = CliRunner()

    published = runner.invoke(app, ["eval", "publish", str(run)])
    assert published.exit_code == 0, published.output
    assert "README.md: rewrote results" in published.output
    assert "239/240" in (tmp_path / "README.md").read_text()
    assert "No failures" not in (tmp_path / "docs" / "EVALS.md").read_text()

    compared = runner.invoke(app, ["eval", "compare", str(run), "--publish"])
    assert compared.exit_code == 0, compared.output
    assert "docs/EVALS.md: rewrote comparison" in compared.output

    baseline = runner.invoke(app, ["eval", "baseline", str(run)])
    assert baseline.exit_code == 1  # happy-return-in-window is in the CI subset but not stable
    assert "the CI subset needs 3/3" in baseline.output
    assert json.loads((evals / "baseline.json").read_text())["k"] == 3
