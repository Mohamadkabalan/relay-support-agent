"""Published numbers (PLAN.md §9.11): the README table and the generated parts of docs/EVALS.md.

Numbers in the README and in EVALS.md are never typed by hand. Each generated block sits between
markers, `<!-- relay:NAME:start -->` and `<!-- relay:NAME:end -->`, and is rewritten from a
committed `report.json` (or an agreement result). Every block names the run it came from and the
command that produced it.

- `results`: the headline of one run (README and EVALS.md).
- `categories`, `judge`, `triage`: the same run, in more detail (EVALS.md).
- `comparison`: several runs side by side, for the effort sweep and the model comparison.
- `agreement`: the judge against the human labels.
"""

import re
from pathlib import Path
from typing import Any

from relay.evals.report import pct, share, triage_counts, usd

START = "<!-- relay:{name}:start -->"
END = "<!-- relay:{name}:end -->"


class MarkerError(ValueError):
    pass


def replace_block(text: str, name: str, content: str) -> str:
    start, end = START.format(name=name), END.format(name=name)
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise MarkerError(f"no {start} ... {end} markers")
    return pattern.sub(lambda _: f"{start}\n{content.rstrip()}\n{end}", text, count=1)


def rewrite(path: Path, blocks: dict[str, str]) -> list[str]:
    """Rewrite the named blocks that `path` has markers for; returns the ones it rewrote."""
    text = path.read_text("utf-8")
    written = []
    for name, content in blocks.items():
        if START.format(name=name) in text:
            text = replace_block(text, name, content)
            written.append(name)
    path.write_text(text, "utf-8")
    return written


def source(report: dict[str, Any]) -> str:
    config = report["config"]
    dirty = " with uncommitted changes" if config["git_dirty"] else ""
    return (
        f"Run `{config['run_id']}` (git {config['git_sha']}{dirty}), "
        f"`{config['command']}`. Report: `evals/reports/{config['run_id']}/report.md`."
    )


def results_block(report: dict[str, Any]) -> str:
    agg, config = report["aggregates"], report["config"]
    passk = agg["pass_k"]
    low, high = passk["ci95"]
    judge = agg.get("judge") or {}
    rows = [
        ("Task success (pass^1)", share(agg["task_success"])),
        (
            f"pass^{passk['k']}",
            f"{passk['all_pass']}/{passk['scenarios']} scenarios = {pct(passk['rate'])} "
            f"(95% CI {pct(low)} to {pct(high)})",
        ),
        ("Trials with a critical violation", share(agg["violations"]["critical"])),
        ("Trials with a high violation", share(agg["violations"]["high"])),
        ("Trials where the tool layer blocked an attempt", share(agg["attempted_violations"])),
        (
            "Judge: all criteria pass",
            share(judge["all_pass"]) if judge.get("judged") else "not run",
        ),
        (
            "Agent cost per conversation",
            f"mean {usd(agg['cost_usd']['agent_mean'])}, p90 {usd(agg['cost_usd']['agent_p90'])}",
        ),
        (
            "Time to first token per turn",
            f"p50 {agg['latency_ms']['ttft_p50']} ms, p95 {agg['latency_ms']['ttft_p95']} ms",
        ),
    ]
    head = (
        f"`{config['agent_model']}` at effort {config['agent_effort']}, {agg['scenarios']} "
        f"scenarios x k={config['k']} = {agg['trials']} trials. {source(report)}"
    )
    table = ["| Metric | Value |", "|---|---|", *(f"| {a} | {b} |" for a, b in rows)]
    return head + "\n\n" + "\n".join(table)


def categories_block(report: dict[str, Any]) -> str:
    agg = report["aggregates"]
    lines = ["| Category | Task success |", "|---|---|"]
    lines += [f"| {name} | {share(block)} |" for name, block in agg["by_category"].items()]
    return "\n".join(lines) + "\n\n" + source(report)


def judge_block(report: dict[str, Any]) -> str:
    judge = report["aggregates"].get("judge") or {}
    if not judge.get("judged"):
        return "The judge did not run in " + source(report)
    lines = ["| Criterion | Passed |", "|---|---|"]
    lines += [f"| {key} | {share(block)} |" for key, block in judge["criteria"].items()]
    lines.append(f"| all criteria | {share(judge['all_pass'])} |")
    return "\n".join(lines) + "\n\n" + source(report)


def triage_block(report: dict[str, Any]) -> str:
    counts = triage_counts(report["rows"], report.get("triage") or {})
    if not counts:
        return "No failures in " + source(report)
    lines = ["| Root cause | Failed trials |", "|---|---|"]
    lines += [f"| {cause} | {n} |" for cause, n in counts.items()]
    return "\n".join(lines) + "\n\n" + source(report)


def comparison_block(reports: list[dict[str, Any]]) -> str:
    lines = [
        "| Run | Agent | Effort | Trials | Task success | pass^k | Critical violations | "
        "Judge all pass | Agent cost / conversation | TTFT p50 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        agg, config = report["aggregates"], report["config"]
        judge = agg.get("judge") or {}
        passk = agg["pass_k"]
        lines.append(
            f"| `{config['run_id']}` | `{config['agent_model']}` | {config['agent_effort']} | "
            f"{agg['trials']} | {share(agg['task_success'])} | "
            f"{passk['all_pass']}/{passk['scenarios']} | "
            f"{agg['violations']['critical']['successes']}/{agg['trials']} | "
            f"{share(judge['all_pass']) if judge.get('judged') else 'n/a'} | "
            f"{usd(agg['cost_usd']['agent_mean'])} | {agg['latency_ms']['ttft_p50']} ms |"
        )
    commands = "; ".join(f"`{r['config']['command']}`" for r in reports)
    return "\n".join(lines) + f"\n\nCommands: {commands}. Reports under `evals/reports/`."


def run_blocks(report: dict[str, Any]) -> dict[str, str]:
    return {
        "results": results_block(report),
        "categories": categories_block(report),
        "judge": judge_block(report),
        "triage": triage_block(report),
    }
