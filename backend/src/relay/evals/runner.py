"""A run (PLAN.md §9.1, step 8): every (scenario, trial) pair, concurrently, resumable.

- Up to `concurrency` trials run at once. Rate limits and overloads (429, 529) are retried by the
  SDK with jittered exponential backoff (the eval clients allow more retries than the API's).
- A trial that is aborted (an API still unavailable after those retries, the wall-clock ceiling,
  a harness bug) is written to `errors.jsonl` and tried once more after a jittered pause. It is
  never counted as a failed trial.
- Results are appended to `results.jsonl` as trials finish. `--resume` reads it back and runs
  only the pairs that have no result yet.
"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.evals import report
from relay.evals.scenario import Scenario
from relay.evals.trial import RunSetup, TrialAbortedError, run_trial

ATTEMPTS = 2
RETRY_PAUSE_S = (5.0, 20.0)

Progress = Callable[[str], None]
TrialFn = Callable[[RunSetup, Scenario, int], Awaitable[dict[str, Any]]]


async def execute(
    setup: RunSetup,
    scenarios: list[Scenario],
    *,
    k: int,
    concurrency: int,
    run_dir: Path,
    progress: Progress = lambda line: None,
    trial_fn: TrialFn = run_trial,
    retry_pause_s: tuple[float, float] = RETRY_PAUSE_S,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path, errors_path = run_dir / "results.jsonl", run_dir / "errors.jsonl"
    done = {(r["scenario"], r["trial"]) for r in report.read_jsonl(results_path)}
    todo = [(s, t) for s in scenarios for t in range(1, k + 1) if (s.id, t) not in done]
    if done:
        progress(f"Resuming: {len(done)} trials already graded, {len(todo)} to go.")
    writing = asyncio.Lock()
    gate = asyncio.Semaphore(concurrency)

    async def append(path: Path, row: dict[str, Any]) -> None:
        async with writing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    async def one(scenario: Scenario, trial: int) -> None:
        async with gate:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    row = await trial_fn(setup, scenario, trial)
                except TrialAbortedError as error:
                    await append(
                        errors_path,
                        {
                            "scenario": scenario.id,
                            "trial": trial,
                            "attempt": attempt,
                            "kind": error.kind,
                            "error": str(error),
                            "at": datetime.now(UTC).isoformat(timespec="seconds"),
                        },
                    )
                    progress(f"  ! {scenario.id}.{trial} attempt {attempt}: {error.kind}: {error}")
                    if attempt < ATTEMPTS:
                        await asyncio.sleep(random.uniform(*retry_pause_s))  # noqa: S311
                    continue
                await append(results_path, row)
                mark = "pass" if row["success"] else "FAIL"
                cost = row["agent_cost_micros"] / 1e6
                progress(f"  {mark} {scenario.id}.{trial} ({row['wall_s']} s, ${cost:.4f})")
                return

    await asyncio.gather(*(one(s, t) for s, t in todo))
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return report.write(run_dir)
