"""Putting a run together: settings, the live agent and simulator, the config block, estimates.

The live run uses the production agent settings with two eval-specific ones (§5.4, §9.1):
`prefix_mismatch_behavior: "error"`, so a history edit fails loudly, and more SDK retries,
because 8 concurrent trials hit rate limits that a single chat never does.
"""

import json
import os
import secrets
import subprocess
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from mcp import Client
from pydantic import SecretStr

from relay.agent.manifest import Manifest, ToolDef, digest
from relay.agent.prompts import system_prompt
from relay.agent.providers.anthropic import AnthropicProvider
from relay.config import Settings
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope
from relay.evals.databases import EVAL_NOW, EVAL_SEED, Template
from relay.evals.fixtures import Applied
from relay.evals.judge import RUBRIC_VERSION, Judge
from relay.evals.scenario import Scenario, suite_hash
from relay.evals.simulator import LlmSimulator
from relay.evals.trial import RunSetup
from relay.kb.embeddings import Embedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import build_server, build_tools
from relay.mcp_server.tools import CONFIRM_THEN_COMMIT

EVAL_MAX_RETRIES = 6
SCENARIO_DIRS = ("scenarios", "redteam")
# PLAN.md §9.12, used until a run has measured the real cost. Labeled as an estimate wherever shown.
PLANNED_COST_PER_TRIAL = (0.22, 0.47)
ABLATIONS = {"no_confirmation_rule"}


class MissingKeyError(RuntimeError):
    pass


def evals_root() -> Path:
    """The repository's `evals/` directory, from RELAY_EVALS_DIR or the working directory up."""
    configured = os.environ.get("RELAY_EVALS_DIR")
    if configured:
        return Path(configured)
    for directory in (Path.cwd(), *Path.cwd().parents):
        if (directory / "evals" / "scenarios").is_dir():
            return directory / "evals"
    raise FileNotFoundError("No evals/scenarios directory here or above; set RELAY_EVALS_DIR.")


def scenario_dirs(root: Path) -> list[Path]:
    return [root / name for name in SCENARIO_DIRS]


def with_secret(settings: Settings) -> Settings:
    """Trials serve the MCP app in process; without a configured secret, use a throwaway one."""
    if settings.mcp_jwt_secret is not None:
        return settings
    return settings.model_copy(update={"mcp_jwt_secret": SecretStr(secrets.token_hex(32))})


def live_setup(
    settings: Settings,
    template: Template,
    server_url: str,
    policy: PolicyConfig,
    embedder: Embedder,
    *,
    transcripts: Path | None,
    ablation: str | None = None,
    client: AsyncAnthropic | None = None,  # tests pass one with a fake transport
    judge: bool = True,
) -> RunSetup:
    if client is None:
        if settings.anthropic_api_key is None:
            raise MissingKeyError("Set ANTHROPIC_API_KEY (in .env) to run the evals with Claude.")
        client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=EVAL_MAX_RETRIES,
            timeout=60.0,
        )
    store = settings.store_name

    def make_provider() -> AnthropicProvider:
        return AnthropicProvider(
            client,
            model=settings.agent_model,
            effort=settings.agent_effort,
            fallbacks=settings.agent_fallbacks,
            prefix_mismatch="error",
        )

    def make_simulator(scenario: Scenario, applied: Applied) -> LlmSimulator:
        return LlmSimulator(
            client,
            model=settings.sim_model,
            effort=settings.sim_effort,
            scenario=scenario,
            values=applied.values,
            store_name=store,
        )

    setup = RunSetup(
        settings=with_secret(settings),
        server_url=server_url,
        template=template.name,
        policy=policy,
        embedder=embedder,
        make_provider=make_provider,
        make_simulator=make_simulator,
        transcripts=transcripts,
        judge=make_judge(client, settings, policy) if judge else None,
    )
    return ablate(setup, ablation)


def make_judge(client: AsyncAnthropic, settings: Settings, policy: PolicyConfig) -> Judge:
    return Judge(
        client,
        model=settings.judge_model,
        effort=settings.judge_effort,
        store_name=settings.store_name,
        tools=[tool.name for tool in build_tools(policy)],
    )


def ablate(setup: RunSetup, ablation: str | None) -> RunSetup:
    """The mechanism check's lever (§9.8): the confirmation rule out of the prompt and tools."""
    if ablation is None:
        return setup
    if ablation not in ABLATIONS:
        raise ValueError(f"unknown ablation {ablation!r}; known: {sorted(ABLATIONS)}")
    setup.prompt_transform = drop_confirmation_rule
    setup.manifest_transform = drop_confirmation_docs
    return setup


def drop_confirmation_rule(prompt: str) -> str:
    kept = [line for line in prompt.splitlines() if not line.startswith("- Changes take two steps")]
    if len(kept) == len(prompt.splitlines()):
        raise ValueError("the prompt has no confirmation rule to remove")
    return "\n".join(kept)


def drop_confirmation_docs(manifest: Manifest) -> Manifest:
    return Manifest(
        tuple(
            ToolDef(
                tool.name,
                " ".join(tool.description.replace(CONFIRM_THEN_COMMIT, "").split()),
                tool.input_schema,
                tool.read_only,
                tool.status_label,
            )
            for tool in manifest.tools
        )
    )


async def manifest_hash(policy: PolicyConfig, embedder: Embedder, store_name: str) -> str:
    """The hash conversations will pin, from the tools the server lists (no database needed)."""
    runtime = ToolRuntime(
        sessionmaker=None,  # type: ignore[arg-type]  # listing tools never opens a session
        identity=BoundIdentity(CustomerScope(customer_id=uuid.UUID(int=0))),
        embedder=embedder,
        policy=policy,
    )
    async with Client(build_server(runtime, store_name=store_name)) as client:
        manifest = Manifest.from_mcp((await client.list_tools()).tools)
    return digest(manifest.to_json())


def git_state() -> tuple[str, bool]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown", False
    return sha, dirty


def new_run_dir(root: Path, sha: str) -> tuple[str, Path]:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{sha}"
    return run_id, root / "reports" / run_id


def config_block(
    *,
    run_id: str,
    command: str,
    settings: Settings,
    policy: PolicyConfig,
    scenarios: list[Scenario],
    selection: str,
    k: int,
    concurrency: int,
    template: Template,
    manifest: str,
    ablation: str | None,
    provider: str = "anthropic",
    judge: bool = True,
) -> dict[str, Any]:
    sha, dirty = git_state()
    prompt = system_prompt(policy, settings.store_name)
    prompt_text = drop_confirmation_rule(prompt.text) if ablation else prompt.text
    return {
        "run_id": run_id,
        "command": command,
        "git_sha": sha,
        "git_dirty": dirty,
        "provider": provider,
        "agent_model": settings.agent_model,
        "agent_effort": settings.agent_effort,
        "fallbacks": settings.agent_fallbacks,
        "prefix_mismatch": "error",
        "simulator_model": settings.sim_model,
        "simulator_effort": settings.sim_effort,
        "judge_model": settings.judge_model if judge else None,
        "judge_effort": settings.judge_effort if judge else None,
        "rubric": RUBRIC_VERSION if judge else None,
        "prompt_version": prompt.version,
        "prompt_hash": digest(prompt_text),
        "manifest_hash": manifest,
        "policy_version": policy.version,
        "seed": EVAL_SEED,
        "clock": EVAL_NOW.isoformat(),
        "scenarios": len(scenarios),
        "selection": selection,
        "suite_hash": suite_hash(scenarios),
        "k": k,
        "concurrency": concurrency,
        "template": template.name,
        "template_built": template.built,
        "template_seconds": round(template.seconds, 1),
        "sdks": {name: version(name) for name in ("anthropic", "mcp")},
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "finished_at": None,
        "ablation": ablation,
    }


def estimate(trials: int, reports: Path) -> tuple[float, float, str]:
    """The expected cost of `trials` trials, from the latest measured run if there is one."""
    measured = sorted(reports.glob("*/report.json")) if reports.is_dir() else []
    for path in reversed(measured):
        data = json.loads(path.read_text("utf-8"))
        if data["config"].get("ablation"):
            continue
        per_trial = data["aggregates"]["cost_usd"]["per_trial_mean"]
        if per_trial:
            return (
                per_trial * trials,
                per_trial * trials,
                f"measured in run {data['config']['run_id']}",
            )
    low, high = PLANNED_COST_PER_TRIAL
    return low * trials, high * trials, "an estimate from PLAN.md §9.12; no run has measured it yet"
