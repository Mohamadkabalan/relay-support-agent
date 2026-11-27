"""Aggregates of a run, computed only from its result rows (PLAN.md §9.7).

`aggregate(rows, k)` is a pure function of the rows in `results.jsonl` (and the error count), so
anyone can recompute a report's headline from its raw rows; a test checks that they agree. The
bootstrap uses a fixed seed for the same reason.
"""

import math
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

Z95 = 1.959963984540054
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260915


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The 95% Wilson score interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


def bootstrap(values: Sequence[bool], resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float]:
    """A 95% percentile-bootstrap interval for a mean of booleans (one value per scenario)."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 (reproducible statistics, not secrets)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    return (round(means[int(0.025 * resamples)], 4), round(means[int(0.975 * resamples) - 1], 4))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def rate(successes: int, n: int) -> dict[str, Any]:
    return {
        "successes": successes,
        "n": n,
        "rate": round(successes / n, 4) if n else None,
        "ci95": list(wilson(successes, n)),
    }


def aggregate(rows: list[dict[str, Any]], k: int, errors: int = 0) -> dict[str, Any]:
    n = len(rows)
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)
        by_category[row["category"]].append(row)

    complete = {s: trials for s, trials in by_scenario.items() if len(trials) >= k}
    all_pass = [all(t["success"] for t in trials[:k]) for _, trials in sorted(complete.items())]

    def with_violation(severity: str) -> int:
        return sum(1 for r in rows if any(v["severity"] == severity for v in r["violations"]))

    violation_ids = Counter(v["id"] for r in rows for v in r["violations"])
    attempt_kinds = Counter(a["kind"] for r in rows for a in r["attempts"])
    expected_escalation = [r["checks"]["escalation"]["expected"] for r in rows]
    optional_escalation = [r["checks"]["escalation"].get("optional", False) for r in rows]
    actual_escalation = [r["escalated"] for r in rows]
    tool_calls = sum(r["tool_calls"] for r in rows)
    tool_errors = Counter[str]()
    expected_errors = Counter[str]()
    for row in rows:
        tool_errors.update(row["tool_errors"])
        expected_errors.update(row["expected_tool_errors"])
    agent_costs = [r["agent_cost_micros"] / 1e6 for r in rows]
    sim_costs = [(r["simulator_cost_micros"] or 0) / 1e6 for r in rows]
    judge_costs = [(r.get("judge_cost_micros") or 0) / 1e6 for r in rows]
    ttft = [ms for r in rows for ms in r["turn_ttft_ms"]]
    latency = [ms for r in rows for ms in r["turn_latency_ms"]]
    walls = [r["wall_s"] for r in rows]

    return {
        "trials": n,
        "scenarios": len(by_scenario),
        "k": k,
        "task_success": rate(sum(1 for r in rows if r["success"]), n),
        "pass_k": {
            "k": k,
            "scenarios": len(all_pass),
            "all_pass": sum(all_pass),
            "rate": round(sum(all_pass) / len(all_pass), 4) if all_pass else None,
            "ci95": list(bootstrap(all_pass)),
        },
        "by_category": {
            category: rate(sum(1 for r in trials if r["success"]), len(trials))
            for category, trials in sorted(by_category.items())
        },
        "violations": {
            "critical": rate(with_violation("critical"), n),
            "high": rate(with_violation("high"), n),
            "by_id": dict(sorted(violation_ids.items())),
        },
        "attempted_violations": {
            **rate(sum(1 for r in rows if r["attempts"]), n),
            "by_kind": dict(sorted(attempt_kinds.items())),
        },
        "escalation": {
            **rate(sum(actual_escalation), n),
            "appropriate": sum(
                e and a for e, a in zip(expected_escalation, actual_escalation, strict=True)
            ),
            "unnecessary": sum(
                a and not e and not o
                for e, o, a in zip(
                    expected_escalation, optional_escalation, actual_escalation, strict=True
                )
            ),
            "optional": sum(
                a and o for o, a in zip(optional_escalation, actual_escalation, strict=True)
            ),
            "missed": sum(
                e and not a for e, a in zip(expected_escalation, actual_escalation, strict=True)
            ),
        },
        "turns": {
            "customer_mean": _mean([r["customer_turns"] for r in rows]),
            "model_calls_mean": _mean([r["model_calls"] for r in rows]),
        },
        "tool_errors": {
            "calls": tool_calls,
            "errors": sum(tool_errors.values()),
            "rate": round(sum(tool_errors.values()) / tool_calls, 4) if tool_calls else None,
            "by_code": dict(sorted(tool_errors.items())),
            "expected_by_code": dict(sorted(expected_errors.items())),
        },
        "cost_usd": {
            "agent_mean": _mean(agent_costs, 4),
            "agent_p90": _round(percentile(agent_costs, 90), 4),
            "agent_total": round(sum(agent_costs), 4),
            "agent_complete": all(r["agent_cost_complete"] for r in rows),
            "simulator_mean": _mean(sim_costs, 4),
            "simulator_total": round(sum(sim_costs), 4),
            "judge_total": round(sum(judge_costs), 4),
            "per_trial_mean": _mean(
                [a + s + j for a, s, j in zip(agent_costs, sim_costs, judge_costs, strict=True)],
                4,
            ),
        },
        "latency_ms": {
            "ttft_p50": percentile(ttft, 50),
            "ttft_p95": percentile(ttft, 95),
            "turn_p50": percentile(latency, 50),
            "turn_p95": percentile(latency, 95),
        },
        "time_s": {
            "trial_mean": _mean(walls, 1),
            "trial_p90": percentile(walls, 90),
            "clone_mean_ms": _mean([r["clone_ms"] for r in rows], 0),
        },
        "judge": judge_block(rows),
        "counts": {
            "infrastructure_errors": errors,
            "max_tokens_stops": sum(r["max_tokens_stops"] for r in rows),
            "refusals": sum(r["refusals"] for r in rows),
            "fallback_calls": sum(r["fallback_calls"] for r in rows),
            "simulator_refusals": sum(1 for r in rows if r["simulator_refused"]),
        },
    }


def judge_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Judge pass rates over the trials that got a verdict (§9.6), per criterion and overall."""
    verdicts = [r["judge"] for r in rows if r.get("judge") and "criteria" in r["judge"]]
    errors = sum(1 for r in rows if r.get("judge") and "error" in r["judge"])
    criteria: dict[str, dict[str, Any]] = {}
    for verdict in verdicts:
        for key, item in verdict["criteria"].items():
            block = criteria.setdefault(key, {"passed": 0, "n": 0})
            block["passed"] += int(item["pass"])
            block["n"] += 1
    costs = [(r.get("judge_cost_micros") or 0) / 1e6 for r in rows if r.get("judge")]
    return {
        "judged": len(verdicts),
        "errors": errors,
        "rubrics": sorted({v["rubric"] for v in verdicts}),
        "all_pass": rate(sum(1 for v in verdicts if v["all_pass"]), len(verdicts)),
        "criteria": {
            key: rate(block["passed"], block["n"]) for key, block in sorted(criteria.items())
        },
        "unbacked_claims": sum(len(v.get("unbacked_claims", [])) for v in verdicts),
        "cost_total": round(sum(costs), 4),
        "cost_mean": _mean(costs, 4),
    }


def _mean(values: Sequence[float], digits: int = 2) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
