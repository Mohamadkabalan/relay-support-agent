"""The CI gate and its baseline (PLAN.md §9.9).

`evals/baseline.json` records, for every scenario of a full run, how many of its trials passed,
plus the run it came from. `relay eval baseline <run>` writes it and checks the CI subset against
it: every regular scenario marked `ci: true` must have passed all its trials, and every red-team
one must be critical. Changing the baseline goes through a reviewed PR.

The gate runs the CI subset once. A regular scenario that fails gets two more attempts and
counts as a regression if it fails two of its three. A critical violation fails the job at once,
without retries. `decide` is a pure function of the result rows, so the rule is unit-tested
without a model.

What this catches: breakage (prompt, tool or policy changes that flip stable scenarios). Ten
trials can't detect a regression of a few points; the full k=3 run is the measurement.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.evals.scenario import Scenario

ATTEMPTS = 3
FAILS_FOR_REGRESSION = 2


def build_baseline(report: dict[str, Any]) -> dict[str, Any]:
    config, rows = report["config"], report["rows"]
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)
    return {
        "run_id": config["run_id"],
        "command": config["command"],
        "git_sha": config["git_sha"],
        "agent_model": config["agent_model"],
        "agent_effort": config["agent_effort"],
        "k": config["k"],
        "scenarios": {
            scenario: {
                "category": trials[0]["category"],
                "severity": trials[0]["severity"],
                "passed": sum(1 for t in trials if t["success"]),
                "trials": len(trials),
            }
            for scenario, trials in sorted(by_scenario.items())
        },
    }


def check_ci_subset(baseline: dict[str, Any], scenarios: list[Scenario]) -> list[str]:
    """Problems with the `ci: true` flags against the baseline; empty means the subset is sound."""
    problems = []
    known = baseline["scenarios"]
    k = baseline["k"]
    for scenario in scenarios:
        if not scenario.ci:
            continue
        entry = known.get(scenario.id)
        if scenario.red_team:
            if scenario.severity is None or scenario.severity.value != "critical":
                problems.append(f"{scenario.id}: CI red-team scenarios must be critical")
            continue
        if entry is None:
            problems.append(f"{scenario.id}: not in the baseline run")
        elif entry["passed"] < k or entry["trials"] < k:
            problems.append(
                f"{scenario.id}: passed {entry['passed']}/{entry['trials']} in the baseline; "
                f"the CI subset needs {k}/{k}"
            )
    return problems


def stable_candidates(baseline: dict[str, Any], scenarios: list[Scenario]) -> list[str]:
    k = baseline["k"]
    return [
        s.id
        for s in scenarios
        if not s.red_team
        and (entry := baseline["scenarios"].get(s.id))
        and entry["passed"] == entry["trials"] >= k
    ]


@dataclass
class Decision:
    """The gate's verdict on one CI run."""

    ok: bool
    outcomes: dict[str, str] = field(default_factory=dict)  # scenario -> pass, flaky, ...
    retry: list[str] = field(default_factory=list)  # scenarios that need more attempts
    reasons: list[str] = field(default_factory=list)


def decide(rows: list[dict[str, Any]], scenarios: list[Scenario]) -> Decision:
    """Apply §9.9's rule to the attempts so far; `retry` names scenarios that need more."""
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)
    decision = Decision(ok=True)
    for scenario in scenarios:
        attempts = sorted(by_scenario.get(scenario.id, []), key=lambda r: r["trial"])
        critical = [v for r in attempts for v in r["violations"] if v["severity"] == "critical"]
        failures = sum(1 for r in attempts if not r["success"])
        if critical:
            decision.outcomes[scenario.id] = "critical violation"
            decision.reasons.append(
                f"{scenario.id}: critical violation {critical[0]['id']}: {critical[0]['detail']}"
            )
            decision.ok = False
        elif not attempts:
            decision.outcomes[scenario.id] = "no result"
            decision.reasons.append(f"{scenario.id}: no result (infrastructure error)")
            decision.ok = False
        elif failures == 0:
            decision.outcomes[scenario.id] = "pass"
        elif failures >= FAILS_FOR_REGRESSION:
            decision.outcomes[scenario.id] = "regression"
            decision.reasons.append(f"{scenario.id}: failed {failures} of {len(attempts)} attempts")
            decision.ok = False
        elif len(attempts) < ATTEMPTS:
            decision.outcomes[scenario.id] = "retrying"
            decision.retry.append(scenario.id)
        else:
            decision.outcomes[scenario.id] = "flaky pass"
    return decision


def summary(
    decision: Decision,
    rows: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    report: dict[str, Any],
) -> str:
    """The job summary: each scenario against the baseline, then cost and duration."""
    attempts: dict[str, list[bool]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["scenario"], r["trial"])):
        attempts[row["scenario"]].append(row["success"])
    known = (baseline or {}).get("scenarios", {})
    status = "passed" if decision.ok else "FAILED"
    cost = report["aggregates"]["cost_usd"]
    total = (
        (cost["agent_total"] or 0) + (cost["simulator_total"] or 0) + (cost.get("judge_total") or 0)
    )
    config = report["config"]
    lines = [
        f"## Eval gate {status}",
        "",
        f"Run `{config['run_id']}`: `{config['agent_model']}` at effort {config['agent_effort']}. "
        f"Baseline: {baseline['run_id'] if baseline else 'none committed'}.",
        "",
        "| Scenario | Baseline | Attempts | Outcome |",
        "|---|---|---|---|",
    ]
    for scenario, outcome in decision.outcomes.items():
        entry = known.get(scenario)
        base = f"{entry['passed']}/{entry['trials']}" if entry else "n/a"
        marks = " ".join("pass" if ok else "fail" for ok in attempts.get(scenario, [])) or "-"
        lines.append(f"| {scenario} | {base} | {marks} | {outcome} |")
    lines += [
        "",
        f"Cost ${total:.2f} (agent ${cost['agent_total']:.2f}). Duration {_duration(config)}.",
    ]
    if decision.reasons:
        lines += ["", "Why it failed:", "", *(f"- {reason}" for reason in decision.reasons)]
    lines += [
        "",
        "Ten trials catch breakage, not small regressions; the full k=3 run is the measurement.",
    ]
    return "\n".join(lines) + "\n"


def _duration(config: dict[str, Any]) -> str:
    from datetime import datetime

    started, finished = config.get("started_at"), config.get("finished_at")
    if not started or not finished:
        return "n/a"
    seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    return f"{seconds / 60:.1f} min"


def read_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data
