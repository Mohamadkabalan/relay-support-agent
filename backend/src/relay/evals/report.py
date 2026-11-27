"""`report.md` and `report.json` for a run (PLAN.md §9.11).

`report.json` holds the configuration, every result row, every error row and the aggregates; the
aggregates are recomputed from the rows by `metrics.aggregate`, never edited by hand. `report.md`
is generated from `report.json` alone.
"""

import json
from pathlib import Path
from typing import Any

from relay.evals.metrics import aggregate

ROOT_CAUSES = (
    "agent reasoning",
    "prompt gap",
    "tool design",
    "policy gap",
    "simulator error",
    "scenario bug",
    "infrastructure",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def build(run_dir: Path) -> dict[str, Any]:
    """Assemble `report.json` from the run's config, results and errors."""
    config = json.loads((run_dir / "config.json").read_text("utf-8"))
    rows = sorted(read_jsonl(run_dir / "results.jsonl"), key=lambda r: (r["scenario"], r["trial"]))
    errors = read_jsonl(run_dir / "errors.jsonl")
    unresolved = _unresolved(rows, errors)
    return {
        "config": config,
        "aggregates": aggregate(rows, config["k"], errors=len(unresolved)),
        "rows": rows,
        "errors": errors,
        "unresolved_errors": unresolved,
        "triage": read_triage(run_dir),
    }


def read_triage(run_dir: Path) -> dict[str, dict[str, str]]:
    """Root-cause labels from `relay eval triage`: "scenario.trial" -> {cause, note}."""
    path = run_dir / "triage.json"
    if not path.exists():
        return {}
    labels: dict[str, dict[str, str]] = json.loads(path.read_text("utf-8"))
    return labels


def write(run_dir: Path) -> dict[str, Any]:
    report = build(run_dir)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (run_dir / "report.md").write_text(markdown(report))
    return report


def _unresolved(rows: list[dict[str, Any]], errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Errors of trials that never produced a result (a retry may have succeeded)."""
    done = {(r["scenario"], r["trial"]) for r in rows}
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for error in errors:
        key = (error["scenario"], error["trial"])
        if key not in done:
            latest[key] = error
    return [latest[key] for key in sorted(latest)]


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def share(block: dict[str, Any]) -> str:
    low, high = block["ci95"]
    if not block["n"]:
        return "n/a"
    rate = pct(block["rate"])
    return f"{block['successes']}/{block['n']} = {rate} (95% CI {pct(low)} to {pct(high)})"


def usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def table(rows: list[tuple[str, str]], header: tuple[str, str]) -> list[str]:
    return [f"| {header[0]} | {header[1]} |", "|---|---|", *(f"| {a} | {b} |" for a, b in rows)]


def configuration(config: dict[str, Any]) -> list[tuple[str, str]]:
    dirty = " (uncommitted changes)" if config["git_dirty"] else ""
    agent = (
        f"{config['provider']} `{config['agent_model']}`, effort {config['agent_effort']}, "
        f"fallbacks {config['fallbacks']}, prefix mismatch `{config['prefix_mismatch']}`"
    )
    template = "built" if config["template_built"] else "reused"
    return [
        ("Command", f"`{config['command']}`"),
        ("Git", f"{config['git_sha']}{dirty}"),
        ("Agent", agent),
        (
            "Simulated customer",
            f"`{config['simulator_model']}`, effort {config['simulator_effort']}",
        ),
        ("Prompt", f"{config['prompt_version']} ({config['prompt_hash'][:12]})"),
        ("Tool manifest", str(config.get("manifest_hash") or "n/a")[:12]),
        ("Policy", config["policy_version"]),
        ("Seed and clock", f"{config['seed']}, {config['clock']}"),
        (
            "Scenarios",
            f"{config['scenarios']} ({config['selection']}), suite {config['suite_hash']}",
        ),
        ("Trials", f"k={config['k']}, concurrency {config['concurrency']}"),
        (
            "Judge",
            f"`{config['judge_model']}`, effort {config['judge_effort']}, rubric {config['rubric']}"
            if config.get("judge_model")
            else "off",
        ),
        ("Template", f"{config['template']} ({template}, {config['template_seconds']:.1f} s)"),
        ("SDKs", ", ".join(f"{name} {v}" for name, v in config["sdks"].items())),
        ("Started", config["started_at"]),
        ("Finished", config.get("finished_at") or "n/a"),
    ]


def headline(agg: dict[str, Any]) -> list[tuple[str, str]]:
    passk, escalation = agg["pass_k"], agg["escalation"]
    cost, latency, time_s = agg["cost_usd"], agg["latency_ms"], agg["time_s"]
    low, high = passk["ci95"]
    incomplete = "" if cost["agent_complete"] else " (incomplete: a model had no price)"
    return [
        ("Task success (pass^1)", share(agg["task_success"])),
        (
            f"pass^{passk['k']} (every trial of a scenario passed)",
            f"{passk['all_pass']}/{passk['scenarios']} scenarios = {pct(passk['rate'])} "
            f"(95% bootstrap CI {pct(low)} to {pct(high)})",
        ),
        ("Trials with a critical violation", share(agg["violations"]["critical"])),
        ("Trials with a high violation", share(agg["violations"]["high"])),
        ("Trials with a blocked attempt (A1)", share(agg["attempted_violations"])),
        (
            "Escalated",
            f"{share(escalation)}; appropriate {escalation['appropriate']}, "
            f"unnecessary {escalation['unnecessary']}, missed {escalation['missed']}",
        ),
        (
            "Customer messages per conversation",
            f"{agg['turns']['customer_mean']} (model calls {agg['turns']['model_calls_mean']})",
        ),
        (
            "Agent cost per conversation",
            f"mean {usd(cost['agent_mean'])}, p90 {usd(cost['agent_p90'])}{incomplete}",
        ),
        ("Simulated-customer cost per conversation", f"mean {usd(cost['simulator_mean'])}"),
        (
            "Turn latency",
            f"time to first token p50 {latency['ttft_p50']} ms, p95 {latency['ttft_p95']} ms; "
            f"whole turn p50 {latency['turn_p50']} ms, p95 {latency['turn_p95']} ms",
        ),
        (
            "Run cost",
            f"agent {usd(cost['agent_total'])}, simulated customer {usd(cost['simulator_total'])}, "
            f"judge {usd(cost.get('judge_total'))}",
        ),
        (
            "Trial time",
            f"mean {time_s['trial_mean']} s, p90 {time_s['trial_p90']} s; "
            f"database clone mean {time_s['clone_mean_ms']} ms",
        ),
        (
            "Infrastructure errors",
            f"{agg['counts']['infrastructure_errors']} trials without a result",
        ),
        ("Judge: trials passing all criteria", judge_summary(agg.get("judge"))),
    ]


def judge_summary(judge: dict[str, Any] | None) -> str:
    if not judge or not (judge["judged"] or judge["errors"]):
        return "not run"
    errors = f"; {judge['errors']} judge errors" if judge["errors"] else ""
    return f"{share(judge['all_pass'])}{errors}"


def markdown(report: dict[str, Any]) -> str:
    config, agg = report["config"], report["aggregates"]
    rows: list[dict[str, Any]] = report["rows"]
    attempted = agg["attempted_violations"]
    lines = [f"# Eval run {config['run_id']}", ""]
    if config.get("ablation"):
        lines += [
            f"> **Ablation run:** {config['ablation']}. Not comparable with regular runs.",
            "",
        ]
    lines += ["## Configuration", "", *table(configuration(config), ("Setting", "Value"))]
    lines += [
        "",
        "## Headline",
        "",
        "Rates are over graded trials. Trials that hit an infrastructure error are kept out and "
        "listed below.",
        "",
        *table(headline(agg), ("Metric", "Value")),
        "",
        "## By category",
        "",
        "| Category | Task success |",
        "|---|---|",
    ]
    lines += [f"| {category} | {share(block)} |" for category, block in agg["by_category"].items()]

    lines += [
        "",
        "## Per scenario",
        "",
        "| Scenario | Category | Passed | Failing checks |",
        "|---|---|---|---|",
    ]
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_scenario.setdefault(row["scenario"], []).append(row)
    for scenario, trials in by_scenario.items():
        passed = sum(1 for t in trials if t["success"])
        problems = sorted({p for t in trials for p in failing(t)})
        notes = "; ".join(problems) or "-"
        lines.append(f"| {scenario} | {trials[0]['category']} | {passed}/{len(trials)} | {notes} |")

    judge = agg.get("judge")
    if judge and judge["judged"]:
        lines += [
            "",
            "## Judge",
            "",
            f"Rubric {', '.join(judge['rubrics'])}; {judge['judged']} trials judged, "
            f"{judge['errors']} without a verdict. Reported apart from task success (§9.6); "
            "only claimed actions the database doesn't back (V4) count as violations.",
            "",
            "| Criterion | Passed |",
            "|---|---|",
            *(f"| {key} | {share(block)} |" for key, block in judge["criteria"].items()),
            f"| **all criteria** | {share(judge['all_pass'])} |",
        ]

    lines += ["", "## Violations", ""]
    found = [(r, v) for r in rows for v in r["violations"]]
    lines += [
        f"- {r['scenario']}.{r['trial']}: {v['id']} ({v['severity']}): {v['detail']}"
        for r, v in found
    ] or ["None."]

    lines += ["", "## Blocked attempts (A1)", ""]
    kinds = attempted["by_kind"]
    lines += [f"- {kind}: {count}" for kind, count in kinds.items()] or ["None."]

    tools = agg["tool_errors"]
    lines += [
        "",
        "## Tool errors",
        "",
        f"{tools['errors']} unexpected errors in {tools['calls']} tool calls "
        f"({pct(tools['rate'])}).",
        "",
    ]
    lines += [f"- {code}: {count}" for code, count in tools["by_code"].items()]
    if tools["expected_by_code"]:
        lines += ["", "Errors the scenarios provoke on purpose:", ""]
        lines += [f"- {code}: {count}" for code, count in tools["expected_by_code"].items()]

    lines += ["", "## Infrastructure errors", ""]
    lines += [
        f"- {e['scenario']}.{e['trial']} (attempt {e['attempt']}, {e['kind']}): {e['error']}"
        for e in report["errors"]
    ] or ["None."]

    counts = agg["counts"]
    lines += [
        "",
        "## Other counts",
        "",
        f"- `max_tokens` stops: {counts['max_tokens_stops']}",
        f"- Refusals: {counts['refusals']}",
        f"- Fallback-served model calls: {counts['fallback_calls']}",
        f"- Simulated-customer refusals: {counts['simulator_refusals']}",
        "",
        "## Failures",
        "",
        "Root causes come from `relay eval triage` (one of: " + ", ".join(ROOT_CAUSES) + ").",
        "",
        "| Trial | What failed | Root cause |",
        "|---|---|---|",
    ]
    triage = report.get("triage") or {}
    for r in rows:
        if r["success"]:
            continue
        key = f"{r['scenario']}.{r['trial']}"
        label = triage.get(key)
        cause = f"{label['cause']}: {label['note']}" if label and label.get("note") else ""
        cause = cause or (label["cause"] if label else "not triaged")
        lines.append(f"| {key} | {_cell('; '.join(failing(r)))} | {_cell(cause)} |")
    counts = triage_counts(rows, triage)
    if counts:
        lines += ["", "Root causes: " + ", ".join(f"{c} {n}" for c, n in counts.items()) + "."]
    return "\n".join(lines) + "\n"


def triage_counts(rows: list[dict[str, Any]], triage: dict[str, dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row["success"]:
            continue
        label = triage.get(f"{row['scenario']}.{row['trial']}")
        cause = label["cause"] if label else "not triaged"
        counts[cause] = counts.get(cause, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def failing(row: dict[str, Any]) -> list[str]:
    checks = row["checks"]
    problems = []
    db = checks["db"]
    problems += [f"missing {item}" for item in db["missing"]]
    problems += [f"unexpected {item}" for item in db["unexpected"]]
    escalation = checks["escalation"]
    if not escalation["ok"]:
        problems.append(f"escalated={escalation['actual']}, expected {escalation['expected']}")
    problems += [
        f"conversation {json.dumps(c['check'], sort_keys=True)}"
        for c in checks["conversation"]
        if not c["ok"]
    ]
    terminal = checks["terminal"]
    if not terminal["ok"]:
        problems.append(f"ended {terminal['stop_reason']} {terminal['failed_turns'] or ''}".strip())
    problems += [f"{v['id']}: {v['detail']}" for v in row["violations"]]
    return problems
