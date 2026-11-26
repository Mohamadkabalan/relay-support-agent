"""Smoke test of the stdio server Claude Desktop launches: a real subprocess on a real database."""

import asyncio
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.factories import NOW, make_engine, temporary_database

from relay.domain.address import Address
from relay.seed import builders

RELAY = str(Path(sys.executable).with_name("relay"))
COMMAND = [RELAY, "mcp", "serve", "--transport", "stdio"]


def server_env(database_url: str, customer: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:1/0"),
        "RELAY_MCP_CUSTOMER": customer,
        "RELAY_LOG_LEVEL": "WARNING",
    }


async def test_claude_desktop_can_call_a_tool_over_stdio() -> None:
    async with temporary_database() as url:
        engine = make_engine(url)
        try:
            async with AsyncSession(engine) as session, session.begin():
                session.add(
                    builders.customer(
                        builders.Ids(),
                        email="stdio.smoke@example.com",
                        full_name="Stdio Smoke",
                        created_at=NOW,
                        address=Address(
                            recipient_name="Stdio Smoke",
                            line1="1 Pipe Street",
                            city="Austin",
                            state="TX",
                            postal_code="78701",
                        ),
                    )
                )
        finally:
            await engine.dispose()

        server = StdioServerParameters(
            command=COMMAND[0],
            args=COMMAND[1:],
            env=server_env(url, "Stdio.Smoke@example.com"),  # emails match case-insensitively
        )
        async with Client(server, read_timeout_seconds=60) as client:
            tools = await client.list_tools()
            result = await client.call_tool("get_customer", {})

    assert len(tools.tools) == 9
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["full_name"] == "Stdio Smoke"


async def test_stdio_refuses_to_start_for_an_unknown_customer() -> None:
    async with temporary_database() as url:
        process = await asyncio.create_subprocess_exec(
            *COMMAND,
            env={**os.environ, **server_env(url, "nobody@example.com")},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)

    assert process.returncode == 1
    assert stdout == b""  # stdout belongs to the protocol
    assert "No customer 'nobody@example.com'" in stderr.decode()
