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


Subset = Annotated[str, typer.Option(help="Scenario subset: v1, ci (the CI subset), or all.")]
Ids = Annotated[
    list[str] | None, typer.Option("--scenario", help="Only these scenario ids (repeatable).")
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
    subset: Subset = "v1",
    scenario: Ids = None,
    k: Annotated[int, typer.Option(min=1, help="Trials per scenario.")] = 3,
    concurrency: Annotated[int, typer.Option(min=1, help="Trials at once.")] = 8,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
    resume: Annotated[
        Path | None, typer.Option(help="A run directory to finish: skips graded trials.")
    ] = None,
    rebuild_template: Annotated[bool, typer.Option(help="Rebuild the template DB.")] = False,
) -> None:
    """Run scenarios with the live agent and simulated customer; write report.md and report.json."""
    from relay.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key is None:
        typer.echo("relay eval: Set ANTHROPIC_API_KEY (in .env) to run the evals.", err=True)
        raise typer.Exit(1)
    chosen = _load(subset, scenario)
    flags = "".join(f" --scenario {s}" for s in scenario or [])
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
        )
    )
    _print_headline(report)


@eval_app.command("mechanism")
def mechanism(
    k: Annotated[int, typer.Option(min=1, help="Trials per scenario and variant.")] = 2,
    concurrency: Annotated[int, typer.Option(min=1)] = 8,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before spending.")] = False,
) -> None:
    """Mechanism check (§9.8): without the confirmation rule, blocked early commits must rise."""
    from relay.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key is None:
        typer.echo("relay eval: Set ANTHROPIC_API_KEY.", err=True)
        raise typer.Exit(1)
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
) -> dict[str, Any]:
    from relay.domain.policy_config import default_policy
    from relay.evals import runner
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
    trials = len(scenarios) * k
    low, high, source = estimate(trials, root / "reports")
    span = f"${low:.2f}" if low == high else f"${low:.2f} to ${high:.2f}"
    typer.echo(
        f"{len(scenarios)} scenarios x {k} trials = {trials} trials. Cost: {span} ({source})."
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
    )
    return await runner.execute(
        setup, scenarios, k=k, concurrency=concurrency, run_dir=run_dir, progress=typer.echo
    )


def _print_headline(report: dict[str, Any]) -> None:
    from relay.evals.report import headline

    for label, value in headline(report["aggregates"]):
        typer.echo(f"{label}: {value}")
    typer.echo(f"Report: {report['config']['run_id']}")
