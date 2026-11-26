"""The `relay` command-line tool.

Commands so far: `api serve`, `mcp serve|token|call`, `chat`, `smoke`, `db`, `seed`, `kb` and
`version`. Later phases add `eval` and `demo` (PLAN.md §13).
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from relay import __version__, turn_tokens
from relay.clock import SystemClock
from relay.config import get_settings
from relay.db import migrate
from relay.db.session import engine_from_settings, make_sessionmaker
from relay.domain.money import format_cents
from relay.domain.policy_config import default_policy
from relay.kb.embeddings import Embedder, FastEmbedder
from relay.mcp_server.identity import UnknownCustomerError, bind_customer
from relay.mcp_server.server import serve_stdio

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
    """Run the MCP server over Streamable HTTP (for the agent) or stdio (for Claude Desktop).

    Over stdio the server acts for the customer in RELAY_MCP_CUSTOMER (an email address or ID).
    """
    settings = get_settings()
    if transport is Transport.STDIO:
        try:
            asyncio.run(serve_stdio(settings))
        except (RuntimeError, UnknownCustomerError) as error:
            typer.echo(f"relay mcp serve: {error}", err=True)  # stdout belongs to the protocol
            raise typer.Exit(1) from error
        return
    uvicorn.run(
        "relay.mcp_server.server:create_http_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


Customer = Annotated[
    str, typer.Option("--as", help="Email address or ID of the customer to act for.")
]
Turn = Annotated[int, typer.Option(min=1, help="Turn number. A commit needs a later turn.")]
Conversation = Annotated[
    str | None, typer.Option(help="Conversation ID. Default: a new one each time.")
]


def _turn_token(customer: str, turn: int, conversation: str | None) -> str:
    """Mint a per-turn token the way the API will (Phase 5). For development only."""
    settings = get_settings()
    if settings.mcp_jwt_secret is None:
        typer.echo("Set RELAY_MCP_JWT_SECRET first.", err=True)
        raise typer.Exit(1)
    secret = settings.mcp_jwt_secret.get_secret_value()

    async def work(engine: AsyncEngine) -> str:
        async with make_sessionmaker(engine)() as session:
            scope = await bind_customer(session, customer)
        return turn_tokens.mint(
            secret,
            customer_id=scope.customer_id,
            conversation_id=uuid.UUID(conversation) if conversation else uuid.uuid7(),
            turn_seq=turn,
            now=datetime.now(UTC),
        )

    try:
        return _run(work)
    except UnknownCustomerError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


@mcp_app.command("token")
def mcp_token(customer: Customer, turn: Turn = 1, conversation: Conversation = None) -> None:
    """Print a per-turn token, for calling the HTTP server by hand (development only)."""
    typer.echo(_turn_token(customer, turn, conversation))


@mcp_app.command("call")
def mcp_call(
    tool: Annotated[str, typer.Argument(help="Tool name, e.g. get_order.")],
    arguments: Annotated[str, typer.Argument(help="Tool arguments as a JSON object.")] = "{}",
    customer: Customer = "dana.whitfield@example.com",
    turn: Turn = 1,
    conversation: Conversation = None,
    url: Annotated[str, typer.Option(help="The MCP endpoint.")] = "http://127.0.0.1:8001/mcp",
) -> None:
    """Call one tool over HTTP with a fresh turn token and print the result (development only).

    To confirm a write by hand, preview with --turn 1, then commit with --turn 2 and the same
    --conversation, passing the confirmation_token from the preview in ARGUMENTS.
    """
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp_types import TextContent

    token = _turn_token(customer, turn, conversation)

    async def call() -> tuple[bool, str]:
        headers = {"Authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http_client,
            Client(streamable_http_client(url, http_client=http_client)) as client,
        ):
            result = await client.call_tool(tool, json.loads(arguments))
        text = "".join(block.text for block in result.content if isinstance(block, TextContent))
        return result.is_error, text

    failed, text = asyncio.run(call())
    typer.echo(text, err=failed)
    if failed:
        raise typer.Exit(1)


# ---------------------------------------------------------------- agent


@app.command()
def chat(
    customer: Customer = "dana.whitfield@example.com",
    in_process: Annotated[
        bool,
        typer.Option(
            "--in-process",
            help="Run the tools inside this process instead of calling the mcp service. "
            "Use it outside the compose network, e.g. on the host.",
        ),
    ] = False,
) -> None:
    """Chat with the agent in the terminal, as a seeded customer (needs a model API key)."""
    from relay.agent.chat import TerminalView, run_chat, stdout_write
    from relay.agent.factory import (
        MissingCredentialsError,
        build_agent,
        build_provider,
        http_tools,
        in_process_tools,
    )
    from relay.agent.tools import ToolConnector

    settings = get_settings()
    view = TerminalView(stdout_write)
    try:
        provider = build_provider(settings)
        tools: ToolConnector | None = None if in_process else http_tools(settings)
    except MissingCredentialsError as error:
        typer.echo(f"relay chat: {error}", err=True)
        raise typer.Exit(1) from error

    async def work(engine: AsyncEngine) -> None:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            scope = await bind_customer(session, customer)
        connector = tools or in_process_tools(
            sessionmaker,
            FastEmbedder(),
            clock=SystemClock(),
            policy=default_policy(),
            store_name=settings.store_name,
        )
        agent = build_agent(
            settings, sessionmaker, provider=provider, tools=connector, listener=view.on_event
        )
        await run_chat(agent, scope.customer_id, view)

    try:
        _run(work)
    except UnknownCustomerError as error:
        typer.echo(f"relay chat: {error}", err=True)
        raise typer.Exit(1) from error


@app.command()
def smoke() -> None:
    """One live conversation with Claude on a throwaway database, checked end to end.

    Needs ANTHROPIC_API_KEY and costs a few cents. Exits 1 if any check fails.
    """
    from relay.agent.chat import TerminalView, stdout_write
    from relay.agent.factory import MissingCredentialsError
    from relay.agent.smoke import run_smoke

    try:
        result = asyncio.run(run_smoke(get_settings(), TerminalView(stdout_write)))
    except MissingCredentialsError as error:
        typer.echo(f"relay smoke: {error}", err=True)
        raise typer.Exit(1) from error
    if not result.passed:
        raise typer.Exit(1)


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
