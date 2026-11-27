"""`relay eval ...`: validate, list, run, self-test and report (PLAN.md §9).

Everything runs on the host against the compose Postgres (DATABASE_URL), like the tests. Live
runs need ANTHROPIC_API_KEY and always print an estimate and ask before spending (`--yes` skips
the question).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any

import typer

eval_app = typer.Typer(help="The eval harness (PLAN.md §9).", no_args_is_help=True)


@eval_app.callback()
def _quiet() -> None:
    """Keep per-request logs of the HTTP clients and the MCP SDK out of the progress output."""
    for name in ("httpx", "httpx2", "mcp", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


Subset = Annotated[
    str,
    typer.Option(
        help="Scenarios: all, regular (no red team), red_team, ci (the CI subset), or a named "
        "subset such as v1."
    ),
]
Ids = Annotated[
    list[str] | None, typer.Option("--scenario", help="Only these scenario ids (repeatable).")
]


AgentModel = Annotated[
    str | None, typer.Option(help="Agent model for this run (default: RELAY_AGENT_MODEL).")
]
AgentEffort = Annotated[
    str | None,
    typer.Option(
        help="Agent effort: low, medium, high, xhigh or max (default: RELAY_AGENT_EFFORT)."
    ),
]


def _load(subset: str, ids: list[str] | None) -> list[Any]:
    from relay.evals.harness import evals_root, scenario_dirs
    from relay.evals.scenario import ScenarioError, load_scenarios, select

    try:
        scenarios = load_scenarios(scenario_dirs(evals_root()))
        return select(scenarios, subset=None if subset == "all" else subset, ids=ids)
    except (ScenarioError, FileNotFoundError) as error:
        typer.echo(f"relay eval: {error}", err=True)
        raise typer.Exit(1) from error


@eval_app.command("validate")
def validate() -> None:
    """Parse every scenario and check its references (part of the self-tests)."""
    scenarios = _load("all", None)
    red = sum(1 for s in scenarios if s.red_team)
    typer.echo(f"{len(scenarios)} scenarios ({red} red team) parse; their references resolve.")


@eval_app.command("list")
def list_scenarios(
    subset: Subset = "all",
    markdown: Annotated[bool, typer.Option(help="Print the Markdown overview.")] = False,
) -> None:
    """List the scenarios, or print the Markdown overview kept in evals/SCENARIOS.md."""
    from relay.evals.overview import overview

    scenarios = _load(subset, None)
    if markdown:
        typer.echo(overview(scenarios), nl=False)
        return
    for scenario in scenarios:
        flags = " ci" if scenario.ci else ""
        typer.echo(f"{scenario.id:<48} {scenario.category.value:<18}{flags}")


@eval_app.command("run")
def run(
    subset: Subset = "all",
    scenario: Ids = None,
    k: Annotated[int, typer.Option(min=1, help="Trials per scenario.")] = 3,
    concurrency: Annotated[int, typer.Option(min=1, help="Trials at once.")] = 8,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
    resume: Annotated[
        Path | None, typer.Option(help="A run directory to finish: skips graded trials.")
    ] = None,
    rebuild_template: Annotated[bool, typer.Option(help="Rebuild the template DB.")] = False,
    agent_model: AgentModel = None,
    agent_effort: AgentEffort = None,
    judge: Annotated[bool, typer.Option(help="Score each trial with the judge.")] = True,
) -> None:
    """Run scenarios with the live agent and simulated customer; write report.md and report.json."""
    settings = _live_settings(agent_model, agent_effort)
    chosen = _load(subset, scenario)
    flags = "".join(f" --scenario {s}" for s in scenario or [])
    flags += f" --agent-model {agent_model}" if agent_model else ""
    flags += f" --agent-effort {agent_effort}" if agent_effort else ""
    flags += "" if judge else " --no-judge"
    command = f"relay eval run --subset {subset}{flags} --k {k} --concurrency {concurrency}"
    report = asyncio.run(
        _live(
            settings,
            chosen,
            k=k,
            concurrency=concurrency,
            yes=yes,
            resume=resume,
            rebuild=rebuild_template,
            command=command,
            selection=",".join(scenario) if scenario else subset,
            judge=judge,
        )
    )
    _print_headline(report)


def _live_settings(agent_model: str | None = None, agent_effort: str | None = None) -> Any:
    from relay.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key is None:
        typer.echo("relay eval: Set ANTHROPIC_API_KEY (in .env) to run the evals.", err=True)
        raise typer.Exit(1)
    update: dict[str, Any] = {}
    if agent_model:
        update["agent_model"] = agent_model
    if agent_effort:
        if agent_effort not in ("low", "medium", "high", "xhigh", "max"):
            typer.echo(f"relay eval: unknown effort {agent_effort!r}", err=True)
            raise typer.Exit(2)
        update["agent_effort"] = agent_effort
    return settings.model_copy(update=update) if update else settings


@eval_app.command("mechanism")
def mechanism(
    k: Annotated[int, typer.Option(min=1, help="Trials per scenario and variant.")] = 2,
    concurrency: Annotated[int, typer.Option(min=1)] = 8,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
) -> None:
    """Mechanism check (§9.8): without the confirmation rule, blocked early commits must rise."""
    settings = _live_settings()
    chosen = [s for s in _load("all", None) if "mechanism" in s.tags]
    counts: dict[str, tuple[int, int]] = {}
    for ablation in (None, "no_confirmation_rule"):
        label = ablation or "baseline"
        report = asyncio.run(
            _live(
                settings,
                chosen,
                k=k,
                concurrency=concurrency,
                yes=yes or ablation is not None,
                resume=None,
                rebuild=False,
                command=f"relay eval mechanism --k {k} ({label})",
                selection="mechanism",
                ablation=ablation,
                judge=False,
            )
        )
        early = sum(
            1
            for row in report["rows"]
            for a in row["attempts"]
            if a["code"] == "CONFIRMATION_TOO_EARLY"
        )
        counts[label] = (early, len(report["rows"]))
    for label, (early, trials) in counts.items():
        typer.echo(f"{label:<22} CONFIRMATION_TOO_EARLY attempts: {early} in {trials} trials")
    if counts["no_confirmation_rule"][0] <= counts["baseline"][0]:
        typer.echo("FAIL  removing the rule did not raise blocked early commits", err=True)
        raise typer.Exit(1)
    typer.echo("PASS  the eval sees the confirmation lever")


@eval_app.command("selftest")
def selftest(
    embedder: Annotated[
        str, typer.Option(help="hashing (no model download) or fastembed.")
    ] = "hashing",
    base_seed: Annotated[
        bool, typer.Option(help="Build the template with the full base seed.")
    ] = True,
    concurrency: Annotated[int, typer.Option(min=1)] = 4,
) -> None:
    """Harness self-tests: schema, policy agreement, oracle, null agent, errors. No model calls."""
    from relay.config import get_settings
    from relay.domain.policy_config import default_policy
    from relay.evals.databases import ensure_template
    from relay.evals.harness import with_secret
    from relay.evals.selftest import run_selftest, summary
    from relay.kb.embeddings import Embedder, FastEmbedder, HashingEmbedder

    settings = with_secret(get_settings())
    scenarios = _load("all", None)
    typer.echo(f"Schema: {len(scenarios)} scenarios parse; their references resolve.")
    chosen: Embedder = HashingEmbedder() if embedder == "hashing" else FastEmbedder()
    policy = default_policy()
    server_url = str(settings.database_url)

    async def work() -> list[Any]:
        template = await ensure_template(server_url, policy, chosen, base_seed=base_seed)
        typer.echo(
            f"Template {template.name}: {'built' if template.built else 'reused'} "
            f"in {template.seconds:.1f} s"
        )
        return await run_selftest(
            settings, scenarios, template, server_url, policy, chosen, concurrency=concurrency
        )

    ok, lines = summary(asyncio.run(work()))
    for line in lines:
        typer.echo(line)
    if not ok:
        raise typer.Exit(1)


@eval_app.command("import-drafts")
def import_drafts(
    out: Annotated[
        Path | None, typer.Option(help="Where to write them. Default: evals/scenarios/drafts.")
    ] = None,
) -> None:
    """Write the console's valid scenario drafts as YAML files, for a human to review.

    The loader doesn't read evals/scenarios/drafts/; move a reviewed draft up a level (and
    rename its id and file) to add it to the suite.
    """
    from relay.config import get_settings
    from relay.evals.harness import evals_root

    directory = out or evals_root() / "scenarios" / "drafts"
    written = asyncio.run(_import_drafts(str(get_settings().database_url), directory))
    typer.echo(f"{len(written)} drafts written to {directory}")
    for path in written:
        typer.echo(f"  {path.name}")


async def _import_drafts(url: str, directory: Path) -> list[Path]:
    import yaml
    from sqlalchemy import select

    from relay.db.models import ScenarioDraft
    from relay.db.session import create_engine, make_sessionmaker
    from relay.domain.enums import DraftStatus

    engine = create_engine(url, pool_size=1)
    try:
        async with make_sessionmaker(engine)() as session:
            rows = list(
                await session.scalars(
                    select(ScenarioDraft)
                    .where(ScenarioDraft.status == DraftStatus.VALID)
                    .order_by(ScenarioDraft.created_at)
                )
            )
    finally:
        await engine.dispose()
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for row in rows:
        scenario_id = str(yaml.safe_load(row.yaml or "")["id"])
        path = directory / f"{scenario_id}.yaml"
        note = " ".join(row.note.split())
        header = f"# Draft {row.id} from conversation {row.conversation_id}.\n# Operator: {note}\n"
        path.write_text(header + (row.yaml or ""), "utf-8")
        written.append(path)
    return written


@eval_app.command("report")
def report_command(run_dir: Annotated[Path, typer.Argument(help="A run directory.")]) -> None:
    """Rebuild report.json and report.md from a run's config, results and errors."""
    from relay.evals import report

    _print_headline(report.write(run_dir))


async def _live(
    settings: Any,
    scenarios: list[Any],
    *,
    k: int,
    concurrency: int,
    yes: bool,
    resume: Path | None,
    rebuild: bool,
    command: str,
    selection: str,
    ablation: str | None = None,
    judge: bool = True,
) -> dict[str, Any]:
    from relay.evals import runner

    setup, run_dir = await _prepare(
        settings,
        scenarios,
        k=k,
        concurrency=concurrency,
        yes=yes,
        resume=resume,
        rebuild=rebuild,
        command=command,
        selection=selection,
        ablation=ablation,
        judge=judge,
    )
    return await runner.execute(
        setup, scenarios, k=k, concurrency=concurrency, run_dir=run_dir, progress=typer.echo
    )


async def _prepare(
    settings: Any,
    scenarios: list[Any],
    *,
    k: int,
    concurrency: int,
    yes: bool,
    resume: Path | None,
    rebuild: bool,
    command: str,
    selection: str,
    ablation: str | None = None,
    judge: bool = True,
    trials: int | None = None,
) -> tuple[Any, Path]:
    """Estimate and ask, build or reuse the template, write the config block, build the setup."""
    from relay.domain.policy_config import default_policy
    from relay.evals.databases import ensure_template
    from relay.evals.harness import (
        config_block,
        estimate,
        evals_root,
        git_state,
        live_setup,
        manifest_hash,
        new_run_dir,
    )
    from relay.kb.embeddings import FastEmbedder

    root = evals_root()
    trials = trials or len(scenarios) * k
    low, high, source = estimate(trials, root / "reports")
    span = f"${low:.2f}" if low == high else f"${low:.2f} to ${high:.2f}"
    typer.echo(
        f"{len(scenarios)} scenarios, up to {trials} trials, agent {settings.agent_model} at "
        f"effort {settings.agent_effort}. Cost: {span} ({source})."
    )
    if not yes:
        typer.confirm("Run them?", abort=True)

    policy, embedder = default_policy(), FastEmbedder()
    server_url = str(settings.database_url)
    template = await ensure_template(server_url, policy, embedder, rebuild=rebuild)
    typer.echo(
        f"Template {template.name}: {'built' if template.built else 'reused'} "
        f"({template.seconds:.1f} s)"
    )
    if resume is not None:
        run_dir = resume
    else:
        run_id, run_dir = new_run_dir(root, git_state()[0])
        run_dir.mkdir(parents=True)
        block = config_block(
            run_id=run_id,
            command=command,
            settings=settings,
            policy=policy,
            scenarios=scenarios,
            selection=selection,
            k=k,
            concurrency=concurrency,
            template=template,
            manifest=await manifest_hash(policy, embedder, settings.store_name),
            ablation=ablation,
            judge=judge,
        )
        (run_dir / "config.json").write_text(json.dumps(block, indent=2) + "\n")
    typer.echo(f"Writing to {run_dir}")
    setup = live_setup(
        settings,
        template,
        server_url,
        policy,
        embedder,
        transcripts=run_dir / "transcripts",
        ablation=ablation,
        judge=judge,
    )
    return setup, run_dir


def _print_headline(report: dict[str, Any]) -> None:
    from relay.evals.report import headline

    for label, value in headline(report["aggregates"]):
        typer.echo(f"{label}: {value}")
    typer.echo(f"Report: {report['config']['run_id']}")


# ---------------------------------------------------------------- the judge (§9.6)

LabelDir = Annotated[
    Path | None,
    typer.Option(help="Where the sample and labels live. Default: evals/labels/<run id>."),
]


def _label_dir(run: Path | None, directory: Path | None) -> Path:
    from relay.evals.harness import evals_root

    if directory is not None:
        return directory
    if run is None:
        typer.echo("relay eval: give a run directory or --dir", err=True)
        raise typer.Exit(2)
    return evals_root() / "labels" / run.name


@eval_app.command("label")
def label_command(
    run: Annotated[Path, typer.Argument(help="A run directory with local transcripts.")],
    directory: LabelDir = None,
    size: Annotated[int, typer.Option(min=1, help="Transcripts to sample.")] = 30,
) -> None:
    """Hand-label a stratified sample of a run's transcripts, one criterion at a time.

    The judge's verdicts are hidden while you label. Answers are saved after every transcript;
    run the command again to continue.
    """
    from relay.evals import labels

    target = _label_dir(run, directory)
    if not (target / "sample.json").exists():
        try:
            path = labels.write_sample(run, target, size)
        except ValueError as error:
            typer.echo(f"relay eval: {error}", err=True)
            raise typer.Exit(1) from error
        typer.echo(f"Sampled {size} transcripts into {path}")
    done = labels.label(target, ask=lambda text: str(typer.prompt(text)), show=typer.echo)
    typer.echo(f"Labeled {done} transcripts. Labels: {target / 'labels.json'}")


@eval_app.command("agreement")
def agreement_command(
    directory: Annotated[Path, typer.Argument(help="A labels directory (evals/labels/<run>).")],
    rejudge: Annotated[
        bool, typer.Option(help="Judge the sample again to measure self-agreement (model calls).")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
    publish: Annotated[bool, typer.Option(help="Write the table into docs/EVALS.md.")] = False,
) -> None:
    """Judge against the human labels: accuracy, Cohen's kappa and a confusion matrix."""
    from relay.evals import labels

    if rejudge:
        asyncio.run(_rejudge(directory, yes=yes))
    result = labels.agreement(directory)
    (directory / "agreement.json").write_text(json.dumps(result, indent=2) + "\n")
    text = labels.markdown(result)
    (directory / "agreement.md").write_text(text)
    typer.echo(text)
    if publish:
        _publish_blocks({"agreement": text + f"\nSource: `{directory}/agreement.json`."})
    if not result["all_meet_threshold"]:
        typer.echo("Below target on at least one criterion: revise the rubric (RUBRIC_VERSION) and")
        typer.echo("re-check it on a fresh sample, never on these transcripts.")


async def _rejudge(directory: Path, *, yes: bool) -> None:
    from relay.evals import labels

    settings = _live_settings()
    items = labels.read_json(directory / "sample.json", {"items": []})["items"]
    typer.echo(f"Judging {len(items)} transcripts again with {settings.judge_model}.")
    if not yes:
        typer.confirm("Run them?", abort=True)
    judge = _judge_for(settings)
    again = {}
    for item in items:
        verdict = await judge.score(item["material"])
        again[item["key"]] = verdict.to_json()
    (directory / "rejudge.json").write_text(json.dumps(again, indent=2) + "\n")


def _judge_for(settings: Any) -> Any:
    from anthropic import AsyncAnthropic

    from relay.domain.policy_config import default_policy
    from relay.evals.harness import EVAL_MAX_RETRIES, make_judge

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=EVAL_MAX_RETRIES,
        timeout=120.0,
    )
    return make_judge(client, settings, default_policy())


@eval_app.command("judge-check")
def judge_check(
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
) -> None:
    """Known negatives (§9.6): replies the judge must fail. Five model calls, a few cents."""
    from relay.agent.prompts import system_prompt
    from relay.domain.policy_config import default_policy
    from relay.evals.judge import material, policy_summary
    from relay.evals.labels import KNOWN_NEGATIVES

    settings = _live_settings()
    typer.echo(f"{len(KNOWN_NEGATIVES)} known negatives with {settings.judge_model}.")
    if not yes:
        typer.confirm("Run them?", abort=True)
    judge = _judge_for(settings)
    summary = policy_summary(system_prompt(default_policy(), settings.store_name).text)

    async def work() -> list[tuple[str, str, bool, str]]:
        found = []
        for name, criterion, goal, messages in KNOWN_NEGATIVES:
            verdict = await judge.score(
                material(goal=goal, policy_summary=summary, messages=messages)
            )
            item = verdict.criteria[criterion]
            found.append((name, criterion, not item["pass"], item["reason"]))
        return found

    results = asyncio.run(work())
    for name, criterion, failed, reason in results:
        typer.echo(f"{'PASS' if failed else 'FAIL'}  {name}: {criterion} judged a fail: {failed}")
        typer.echo(f"      {reason}")
    if not all(failed for _, _, failed, _ in results):
        raise typer.Exit(1)


# ---------------------------------------------------------------- triage (§9.11)


@eval_app.command("triage")
def triage(
    run: Annotated[Path, typer.Argument(help="A run directory.")],
    relabel: Annotated[bool, typer.Option(help="Ask again for failures already labeled.")] = False,
) -> None:
    """Label each failed trial with its root cause; the report's failure table shows them."""
    from relay.evals import report
    from relay.evals.report import ROOT_CAUSES, failing, read_triage

    built = report.build(run)
    labels = read_triage(run)
    failures = [r for r in built["rows"] if not r["success"]]
    menu = "  ".join(f"[{i}] {cause}" for i, cause in enumerate(ROOT_CAUSES, start=1))
    for row in failures:
        key = f"{row['scenario']}.{row['trial']}"
        if key in labels and not relabel:
            continue
        typer.echo(f"\n{key}: {'; '.join(failing(row))}")
        transcript = run / "transcripts" / f"{key}.json"
        if transcript.exists():
            typer.echo(f"  transcript: {transcript}")
        typer.echo(f"  {menu}  [s]kip  [q]uit")
        answer = str(typer.prompt("Root cause")).strip().lower()
        if answer == "q":
            break
        if not answer.isdigit() or not 1 <= int(answer) <= len(ROOT_CAUSES):
            continue
        note = str(typer.prompt("Note (what went wrong, the fix)", default="")).strip()
        labels[key] = {"cause": ROOT_CAUSES[int(answer) - 1], "note": note}
        (run / "triage.json").write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    _print_headline(report.write(run))


# ---------------------------------------------------------------- baseline and gate (§9.9)


@eval_app.command("baseline")
def baseline(
    run: Annotated[Path, typer.Argument(help="A full run with k=3, to become the baseline.")],
) -> None:
    """Write evals/baseline.json from a run and check the CI subset against it."""
    from relay.evals import gate, report
    from relay.evals.harness import evals_root

    built = report.build(run)
    if built["config"].get("ablation"):
        typer.echo("relay eval: an ablation run can't be the baseline", err=True)
        raise typer.Exit(1)
    data = gate.build_baseline(built)
    path = evals_root() / "baseline.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    typer.echo(f"Wrote {path} from run {data['run_id']}.")
    scenarios = _load("all", None)
    problems = gate.check_ci_subset(data, scenarios)
    for problem in problems:
        typer.echo(f"  CI subset: {problem}", err=True)
    if problems:
        stable = gate.stable_candidates(data, scenarios)
        typer.echo(f"Regular scenarios that passed every trial: {', '.join(stable) or 'none'}")
        raise typer.Exit(1)
    typer.echo("The CI subset is consistent with the baseline.")


@eval_app.command("gate")
def gate_command(
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
    concurrency: Annotated[int, typer.Option(min=1)] = 8,
) -> None:
    """The CI eval gate: the CI subset once, retries for failures, compared with the baseline."""
    import os

    from relay.evals import gate, report, runner
    from relay.evals.harness import evals_root

    settings = _live_settings()
    chosen = _load("ci", None)

    async def work() -> tuple[gate.Decision, dict[str, Any]]:
        setup, run_dir = await _prepare(
            settings,
            chosen,
            k=1,
            concurrency=concurrency,
            yes=yes,
            resume=None,
            rebuild=False,
            command="relay eval gate",
            selection="ci",
            trials=len(chosen) * gate.ATTEMPTS,
        )
        built = await runner.execute(
            setup, chosen, k=1, concurrency=concurrency, run_dir=run_dir, progress=typer.echo
        )
        decision = gate.decide(built["rows"], chosen)
        if decision.retry:
            typer.echo(f"Retrying {', '.join(decision.retry)}")
            again = [s for s in chosen if s.id in decision.retry]
            await runner.execute(
                setup,
                again,
                k=gate.ATTEMPTS,
                concurrency=concurrency,
                run_dir=run_dir,
                progress=typer.echo,
            )
            built = report.write(run_dir)
            decision = gate.decide(built["rows"], chosen)
        return decision, built

    decision, built = asyncio.run(work())
    baseline_data = gate.read_baseline(evals_root() / "baseline.json")
    text = gate.summary(decision, built["rows"], baseline_data, built)
    typer.echo(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(text)
    if not decision.ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------- comparison and publishing


@eval_app.command("compare")
def compare(
    runs: Annotated[list[Path], typer.Argument(help="Run directories, in the order to show.")],
    publish: Annotated[bool, typer.Option(help="Write the table into docs/EVALS.md.")] = False,
) -> None:
    """Runs side by side: the effort sweep and the model comparison (D2, D3)."""
    from relay.evals import report
    from relay.evals.publish import comparison_block

    text = comparison_block([report.build(run) for run in runs])
    typer.echo(text)
    if publish:
        _publish_blocks({"comparison": text})


@eval_app.command("publish")
def publish_command(run: Annotated[Path, typer.Argument(help="The run to publish.")]) -> None:
    """Rewrite the README results table and the run's sections of docs/EVALS.md."""
    from relay.evals import report
    from relay.evals.publish import run_blocks

    built = report.write(run)
    if built["config"].get("ablation"):
        typer.echo("relay eval: an ablation run is not published", err=True)
        raise typer.Exit(1)
    _publish_blocks(run_blocks(built))


def _publish_blocks(blocks: dict[str, str]) -> None:
    from relay.evals.harness import evals_root
    from relay.evals.publish import rewrite

    repo = evals_root().parent
    for path in (repo / "README.md", repo / "docs" / "EVALS.md"):
        if not path.exists():
            continue
        written = rewrite(path, blocks)
        if written:
            typer.echo(f"{path.relative_to(repo)}: rewrote {', '.join(written)}")
