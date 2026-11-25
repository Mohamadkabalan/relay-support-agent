"""The `relay` command-line tool.

Commands so far: `api serve`, `mcp serve`, `db`, `seed`, `kb` and `version`. Later phases add
`chat`, `eval` and `demo` (PLAN.md §13).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from relay import __version__
from relay.config import get_settings
from relay.db import migrate
from relay.db.session import engine_from_settings, make_sessionmaker
from relay.domain.money import format_cents
from relay.domain.policy_config import default_policy
from relay.kb.embeddings import Embedder, FastEmbedder
from relay.mcp_server.server import build_server

app = typer.Typer(
    help="Relay command-line tools.", no_args_is_help=True, pretty_exceptions_enable=False
)
api_app = typer.Typer(help="The HTTP API.", no_args_is_help=True)
mcp_app = typer.Typer(help="The MCP server.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
kb_app = typer.Typer(help="The help-center knowledge base.", no_args_is_help=True)
app.add_typer(api_app, name="api")
app.add_typer(mcp_app, name="mcp")
app.add_typer(db_app, name="db")
app.add_typer(kb_app, name="kb")

Host = Annotated[str, typer.Option(help="Interface to bind. Containers pass 0.0.0.0.")]
Reload = Annotated[bool, typer.Option(help="Restart when source files change (development).")]


class Transport(StrEnum):
    STREAMABLE_HTTP = "streamable-http"
    STDIO = "stdio"


@app.command()
def version() -> None:
    """Print the Relay version."""
    typer.echo(__version__)


@api_app.command("serve")
def api_serve(host: Host = "127.0.0.1", port: int = 8000, reload: Reload = False) -> None:
    """Run the FastAPI app with uvicorn."""
    settings = get_settings()
    uvicorn.run(
        "relay.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level.lower(),
    )


@mcp_app.command("serve")
def mcp_serve(
    transport: Annotated[Transport, typer.Option(help="How clients connect.")] = (
        Transport.STREAMABLE_HTTP
    ),
    host: Host = "127.0.0.1",
    port: int = 8001,
    reload: Reload = False,
) -> None:
    """Run the MCP server over Streamable HTTP (for the agent) or stdio (for Claude Desktop)."""
    settings = get_settings()
    if transport is Transport.STDIO:
        build_server(settings).run("stdio")
        return
    uvicorn.run(
        "relay.mcp_server.server:create_http_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


# ---------------------------------------------------------------- database


def _run[T](work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = engine_from_settings(get_settings())
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Apply migrations up to REVISION (default: the latest)."""
    _run(lambda engine: migrate.upgrade(engine, revision))
    typer.echo(f"Database at {_run(migrate.current_revision)}.")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument(help="Target, e.g. base.")]) -> None:
    """Revert migrations down to REVISION."""
    _run(lambda engine: migrate.downgrade(engine, revision))
    typer.echo(f"Database at {_run(migrate.current_revision) or 'base'}.")


@db_app.command("current")
def db_current() -> None:
    """Print the applied revision and the latest one."""
    typer.echo(f"applied: {_run(migrate.current_revision) or 'none'}")
    typer.echo(f"head:    {migrate.head_revision()}")


# ---------------------------------------------------------------- seed


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise typer.BadParameter("--now needs a UTC offset, e.g. 2026-09-15T15:00:00-04:00")
    return parsed


def _embedder(enabled: bool) -> Embedder | None:
    return FastEmbedder() if enabled else None


@app.command()
def seed(
    seed: Annotated[int, typer.Option(help="Random seed.")] = 42,
    now: Annotated[
        str | None,
        typer.Option(help="The seed's 'now', ISO 8601 with an offset. Default: the current time."),
    ] = None,
    reset: Annotated[bool, typer.Option(help="Wipe existing data first.")] = False,
    verify: Annotated[
        bool, typer.Option("--verify", help="Only check the existing data; write nothing.")
    ] = False,
    sample: Annotated[int, typer.Option(help="Print this many sample orders.")] = 0,
    embeddings: Annotated[
        bool, typer.Option(help="Embed the help articles (needs the local model).")
    ] = True,
) -> None:
    """Fill the database with the demo store: customers, orders, personas and help articles.

    Without --reset it does nothing if data is already there, so it is safe to run twice.
    """
    from relay.seed.run import seed_database

    policy = default_policy()
    if not verify:
        outcome = _run(
            lambda engine: seed_database(
                engine,
                seed=seed,
                now=_parse_now(now),
                policy=policy,
                embedder=_embedder(embeddings),
                reset=reset,
            )
        )
        if outcome.seeded:
            typer.echo(
                f"Seeded with seed {outcome.seed} at {outcome.seeded_now.isoformat()} "
                f"in {outcome.seconds:.1f} s."
            )
        else:
            typer.echo(
                f"Already seeded (seed {outcome.seed}, now {outcome.seeded_now.isoformat()}); "
                "nothing to do. Pass --reset to start over."
            )
        typer.echo(f"Data hash: {outcome.data_hash}")
    ok = _run(lambda engine: _report(engine, sample))
    if not ok:
        raise typer.Exit(1)


async def _report(engine: AsyncEngine, sample: int) -> bool:
    from relay.seed.load import data_hash
    from relay.seed.verify import sample_orders, verify

    policy = default_policy()
    async with make_sessionmaker(engine)() as session, session.begin():
        report = await verify(session, policy)
        digest = await data_hash(session)
        rows = await sample_orders(session, policy, sample) if sample else []
    for name, counts in report.stats.items():
        total = sum(count for _, count in counts)
        typer.echo(f"\n{name}:")
        for label, count in counts:
            share = f"{100 * count / total:5.1f}%" if total and name == "orders by status" else ""
            typer.echo(f"  {label:<44} {count:>6} {share}")
    if rows:
        typer.echo(f"\nSample of {len(rows)} orders:")
        for number, name, status, shipment, placed_on, total_cents, items in rows:
            state = f"{status}/{shipment}" if shipment else status
            typer.echo(
                f"  {number}  {placed_on}  {state:<28} {format_cents(total_cents):>10}  "
                f"{name}: {items}"
            )
    typer.echo(f"\nData hash now: {digest}")
    if report.ok:
        typer.echo("Verify: all invariants and persona checks pass.")
        return True
    typer.echo("Verify FAILED:", err=True)
    for name, examples in report.failures.items():
        typer.echo(f"  {name}: {', '.join(examples)}", err=True)
    return False


# ---------------------------------------------------------------- knowledge base


@kb_app.command("ingest")
def kb_ingest(
    embeddings: Annotated[bool, typer.Option(help="Embed the chunks.")] = True,
) -> None:
    """Re-render the help articles from policy.yaml and reload them."""
    from relay.kb.ingest import ingest

    async def work(engine: AsyncEngine) -> str:
        async with make_sessionmaker(engine)() as session, session.begin():
            report = await ingest(session, default_policy(), _embedder(embeddings))
        return f"{report.articles} articles, {report.chunks} chunks, embedded: {report.embedded}"

    typer.echo(_run(work))


@kb_app.command("prefetch")
def kb_prefetch() -> None:
    """Download the embedding model into FASTEMBED_CACHE_PATH (the Docker image already has it)."""
    vector = FastEmbedder().embed_query("warm up")
    typer.echo(f"{FastEmbedder.name} ready ({len(vector)} dimensions).")


@kb_app.command("search")
def kb_search(
    query: str,
    mode: Annotated[str, typer.Option(help="fts, vector or hybrid.")] = "vector",
    limit: int = 3,
) -> None:
    """Search the help center the way the agent's tool does."""
    from relay.kb.search import Mode, search

    search_mode = Mode(mode)

    async def work(engine: AsyncEngine) -> None:
        async with make_sessionmaker(engine)() as session:
            hits = await search(
                session,
                query,
                mode=search_mode,
                limit=limit,
                embedder=None if search_mode is Mode.FULL_TEXT else FastEmbedder(),
            )
        for hit in hits:
            typer.echo(f"{hit.score:8.4f}  {hit.slug}  [{hit.heading}]\n          {hit.excerpt}")

    _run(work)


@kb_app.command("eval")
def kb_eval(
    queries: Annotated[Path, typer.Option(help="The labeled queries.")] = Path(
        "../evals/retrieval/queries.yaml"
    ),
    output: Annotated[
        Path | None, typer.Option(help="Also write the Markdown report here.")
    ] = None,
) -> None:
    """Score full-text, vector and hybrid search on labeled customer questions (ADR-0009)."""
    from relay.kb.retrieval_eval import load_queries, run_eval

    labeled = load_queries(queries)

    async def work(engine: AsyncEngine) -> str:
        async with make_sessionmaker(engine)() as session:
            report = await run_eval(session, labeled, FastEmbedder())
        return report.markdown(command=f"relay kb eval --queries {queries}")

    markdown = _run(work)
    typer.echo(markdown)
    if output is not None:
        output.write_text(markdown + "\n", encoding="utf-8")
