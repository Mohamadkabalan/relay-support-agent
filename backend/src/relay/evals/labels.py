"""Validating the judge against human labels (PLAN.md §9.6).

1. `sample` draws 30 trials of a run, stratified by category and outcome, and copies what the
   judge saw (the material) and its verdict into `evals/labels/<run id>/sample.json`.
2. `relay eval label` shows each one in the terminal, without the judge's verdict, and asks a
   person for pass, fail or unsure on each criterion. Answers go to `labels.json` after every
   transcript, so labeling can stop and resume. "Unsure" marks a case that isn't clear-cut.
3. `agreement` compares the labels with the verdicts: accuracy on the clear-cut cases, Cohen's
   kappa and a confusion matrix per criterion. With a second judge pass on the same material
   (`rejudge.json`), it also reports how often the judge agrees with itself.

The labels must come from a person. Nothing here calls a model except the optional re-judging.
"""

import json
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.evals.judge import CRITERIA, RUBRIC_VERSION

SAMPLE_SIZE = 30
SAMPLE_SEED = 20260915
THRESHOLD = 0.9
ANSWERS = {"p": "pass", "f": "fail", "u": "unsure"}


# ---------------------------------------------------------------- the sample


def sample(run_dir: Path, size: int = SAMPLE_SIZE, seed: int = SAMPLE_SEED) -> list[dict[str, Any]]:
    """Up to `size` judged trials, round-robin over (category, success) strata."""
    transcripts = run_dir / "transcripts"
    strata: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(transcripts.glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        row = data["result"]
        verdict = row.get("judge")
        if not verdict or "criteria" not in verdict or "material" not in data:
            continue
        strata[(row["category"], bool(row["success"]))].append(
            {
                "key": f"{row['scenario']}.{row['trial']}",
                "scenario": row["scenario"],
                "category": row["category"],
                "success": row["success"],
                "material": data["material"],
                "judge": verdict,
            }
        )
    rng = random.Random(seed)  # noqa: S311 (a reproducible sample, not a secret)
    groups = [strata[key] for key in sorted(strata)]
    for group in groups:
        rng.shuffle(group)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < size and any(groups):
        for group in groups:
            if group and len(chosen) < size:
                chosen.append(group.pop())
    return sorted(chosen, key=lambda item: item["key"])


def write_sample(run_dir: Path, out: Path, size: int = SAMPLE_SIZE) -> Path:
    items = sample(run_dir, size)
    if not items:
        raise ValueError(f"{run_dir} has no judged transcripts to sample")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "sample.json"
    payload = {"run": run_dir.name, "rubric": RUBRIC_VERSION, "items": items}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------- labeling

Ask = Callable[[str], str]
Show = Callable[[str], None]


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def label(directory: Path, ask: Ask, show: Show) -> int:
    """Ask for labels on every unlabeled transcript. Returns how many were labeled now."""
    items = read_json(directory / "sample.json", {"items": []})["items"]
    labels_path = directory / "labels.json"
    labels: dict[str, Any] = read_json(labels_path, {})
    todo = [item for item in items if item["key"] not in labels]
    done = 0
    for index, item in enumerate(todo, start=1):
        show(f"\n{'=' * 78}\n[{index}/{len(todo)}] {item['key']}\n{'=' * 78}\n")
        show(item["material"])
        answers: dict[str, str] = {}
        for key, text in CRITERIA.items():
            show(f"\n{key}: {text}")
            while True:
                answer = ask("[p]ass, [f]ail, [u]nsure, or [q]uit").strip().lower()[:1]
                if answer == "q":
                    return done
                if answer in ANSWERS:
                    answers[key] = ANSWERS[answer]
                    break
        labels[item["key"]] = {
            "criteria": answers,
            "rubric": RUBRIC_VERSION,
            "labeled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        labels_path.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
        done += 1
    return done


# ---------------------------------------------------------------- agreement


@dataclass(frozen=True, slots=True)
class Pairs:
    """(human, judge) verdicts for one criterion, clear-cut cases only."""

    pairs: list[tuple[bool, bool]]
    unsure: int

    @property
    def n(self) -> int:
        return len(self.pairs)

    def confusion(self) -> dict[str, int]:
        return {
            "human_pass_judge_pass": sum(1 for h, j in self.pairs if h and j),
            "human_pass_judge_fail": sum(1 for h, j in self.pairs if h and not j),
            "human_fail_judge_pass": sum(1 for h, j in self.pairs if not h and j),
            "human_fail_judge_fail": sum(1 for h, j in self.pairs if not h and not j),
        }

    def accuracy(self) -> float | None:
        return round(sum(1 for h, j in self.pairs if h == j) / self.n, 4) if self.n else None

    def kappa(self) -> float | None:
        return cohen_kappa(self.pairs)


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two raters on a yes/no item; None when chance agreement is total."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    a_yes = sum(1 for a, _ in pairs if a) / n
    b_yes = sum(1 for _, b in pairs if b) / n
    expected = a_yes * b_yes + (1 - a_yes) * (1 - b_yes)
    if expected == 1:
        return None
    return round((observed - expected) / (1 - expected), 4)


def agreement(directory: Path) -> dict[str, Any]:
    items = read_json(directory / "sample.json", {"items": []})["items"]
    labels: dict[str, Any] = read_json(directory / "labels.json", {})
    rejudged: dict[str, Any] = read_json(directory / "rejudge.json", {})
    criteria: dict[str, Any] = {}
    for key in CRITERIA:
        pairs, unsure = [], 0
        for item in items:
            human = labels.get(item["key"], {}).get("criteria", {}).get(key)
            judged = item["judge"]["criteria"].get(key)
            if human is None or judged is None:
                continue
            if human == "unsure":
                unsure += 1
                continue
            pairs.append((human == "pass", bool(judged["pass"])))
        found = Pairs(pairs, unsure)
        same, compared = 0, 0
        for item in items:
            again = rejudged.get(item["key"], {}).get("criteria", {}).get(key)
            if again is not None:
                compared += 1
                same += int(bool(again["pass"]) == bool(item["judge"]["criteria"][key]["pass"]))
        accuracy = found.accuracy()
        criteria[key] = {
            "clear_cut": found.n,
            "unsure": found.unsure,
            "accuracy": accuracy,
            "kappa": found.kappa(),
            "confusion": found.confusion(),
            "meets_threshold": accuracy is not None and accuracy >= THRESHOLD,
            "self_agreement": round(same / compared, 4) if compared else None,
            "self_compared": compared,
        }
    rubrics = sorted({item["judge"]["rubric"] for item in items})
    return {
        "run": read_json(directory / "sample.json", {}).get("run"),
        "sample": len(items),
        "labeled": sum(1 for item in items if item["key"] in labels),
        "rubrics": rubrics,
        "threshold": THRESHOLD,
        "criteria": criteria,
        "all_meet_threshold": bool(criteria)
        and all(block["meets_threshold"] for block in criteria.values()),
    }


def markdown(result: dict[str, Any]) -> str:
    def num(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    lines = [
        f"Judge agreement on {result['labeled']} of {result['sample']} hand-labeled transcripts "
        f"from run {result['run']} (rubric {', '.join(result['rubrics']) or 'n/a'}). Accuracy and "
        f"kappa count clear-cut cases only; the target is accuracy of at least "
        f"{result['threshold']:.1f} on every criterion.",
        "",
        "| Criterion | Clear-cut | Unsure | Accuracy | Kappa | Human pass / judge fail | "
        "Human fail / judge pass | Judge self-agreement |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, block in result["criteria"].items():
        confusion = block["confusion"]
        selfie = (
            f"{num(block['self_agreement'])} of {block['self_compared']}"
            if block["self_compared"]
            else "not run"
        )
        mark = "" if block["meets_threshold"] else " (below target)"
        lines.append(
            f"| {key} | {block['clear_cut']} | {block['unsure']} | {num(block['accuracy'])}{mark} "
            f"| {num(block['kappa'])} | {confusion['human_pass_judge_fail']} "
            f"| {confusion['human_fail_judge_pass']} | {selfie} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- known negatives


def _chat(goal: str, *turns: tuple[str, str]) -> tuple[str, list[dict[str, Any]]]:
    kinds = {"customer": "customer", "agent": "assistant"}
    return goal, [{"kind": kinds[who], "content": {"text": text}} for who, text in turns]


# (name, the criterion that must fail, goal, conversation). §9.6: an empty reply, "I don't
# know", and a confident answer to a different question must fail; two more cover invented facts
# and leaked internals.
KNOWN_NEGATIVES: list[tuple[str, str, str, list[dict[str, Any]]]] = [
    (
        "empty reply",
        "addresses_requests",
        *_chat(
            "Find out where order PV-900001 is.",
            ("customer", "Where is my order PV-900001?"),
            ("agent", "."),
        ),
    ),
    (
        "I don't know",
        "addresses_requests",
        *_chat(
            "Learn how long you have to return a refurbished item.",
            ("customer", "How long do I have to return a refurbished laptop?"),
            ("agent", "I don't know."),
        ),
    ),
    (
        "answers a different question",
        "addresses_requests",
        *_chat(
            "Learn how long you have to return a refurbished item.",
            ("customer", "How long do I have to return a refurbished laptop?"),
            (
                "agent",
                "Standard shipping costs $5.99 and is free on orders over $50. Express shipping "
                "is $14.99 and arrives in about 3 days.",
            ),
        ),
    ),
    (
        "invents an amount",
        "consistent_with_tools",
        *_chat(
            "Get a refund for headphones that arrived broken in order PV-900001.",
            ("customer", "My headphones from PV-900001 arrived broken. Can I get a refund?"),
            ("agent", "Done: I've refunded $412.50 to your card. It settles in 3-5 business days."),
        ),
    ),
    (
        "leaks internals",
        "no_internals",
        *_chat(
            "Return a speaker from order PV-900001.",
            ("customer", "Can I return the speaker from PV-900001?"),
            (
                "agent",
                "My create_return tool returned RETURN_WINDOW_EXPIRED (rule RET-2), so I can't.",
            ),
        ),
    ),
]
