"""Binding a stdio server to a customer, and the dev command that mints turn tokens."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import TEST_JWT_SECRET
from tests.support.factories import (
    NOW,
    Factory,
    create_test_database,
    drop_test_database,
    make_engine,
)
from typer.testing import CliRunner

from relay import turn_tokens
from relay.cli import app
from relay.config import get_settings
from relay.db.models import Customer
from relay.domain.address import Address
from relay.mcp_server.identity import UnknownCustomerError, bind_customer
from relay.seed import builders


async def test_bind_customer_by_email_or_id(session: AsyncSession, factory: Factory) -> None:
    customer = await factory.customer()

    by_email = await bind_customer(session, f"  {customer.email.upper()} ")
    by_id = await bind_customer(session, str(customer.id))

    assert by_email.customer_id == by_id.customer_id == customer.id
    assert (by_email.conversation_id, by_email.turn_seq) == (None, None)  # stdio has no turns
    with pytest.raises(UnknownCustomerError, match="RELAY_MCP_CUSTOMER"):
        await bind_customer(session, "nobody@example.com")


def test_mcp_token_command_mints_a_token_for_a_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI runs its own event loop, so this test is synchronous and sets up in separate loops.
    url = asyncio.run(create_test_database())
    try:
        customer = asyncio.run(committed_customer(url, "token.cli@example.com"))
        with monkeypatch.context() as env:  # undone before the drop, which reads DATABASE_URL
            env.setenv("DATABASE_URL", url)
            env.setenv("RELAY_MCP_JWT_SECRET", TEST_JWT_SECRET)
            get_settings.cache_clear()
            runner = CliRunner()
            minted = runner.invoke(
                app, ["mcp", "token", "--as", "token.cli@example.com", "--turn", "4"]
            )
            unknown = runner.invoke(app, ["mcp", "token", "--as", "nobody@example.com"])
    finally:
        get_settings.cache_clear()
        asyncio.run(drop_test_database(url))

    assert minted.exit_code == 0, minted.output
    claims = turn_tokens.verify(minted.output.strip(), TEST_JWT_SECRET)
    assert (claims.customer_id, claims.turn_seq) == (customer.id, 4)
    assert unknown.exit_code == 1


async def committed_customer(url: str, email: str) -> Customer:
    engine = make_engine(url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            row = builders.customer(
                builders.Ids(),
                email=email,
                full_name="Token Cli",
                created_at=NOW,
                address=Address(
                    recipient_name="Token Cli",
                    line1="2 Key Lane",
                    city="Boston",
                    state="MA",
                    postal_code="02110",
                ),
            )
            session.add(row)
        return row
    finally:
        await engine.dispose()
