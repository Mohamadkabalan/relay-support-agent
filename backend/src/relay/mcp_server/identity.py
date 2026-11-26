"""Who the tools act for (PLAN.md §6.1). Identity never comes from tool arguments.

- Over HTTP, every request carries the per-turn JWT the API minted. `TurnTokenVerifier` checks it
  before the request reaches a tool (anything invalid is a 401), and `TokenIdentity` turns the
  verified claims into the `CustomerScope`.
- Over stdio (Claude Desktop), the process is bound to one customer at startup
  (`RELAY_MCP_CUSTOMER`). Whoever launches the process decides who it acts for, so the boundary
  is the machine, as the MCP docs describe for stdio servers.
- Tests bind a fixed scope the same way.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay import turn_tokens
from relay.db.models import Customer
from relay.domain.scope import CustomerScope

logger = logging.getLogger(__name__)


class Identity(Protocol):
    def scope(self) -> CustomerScope:
        """The customer this request acts for. Raises `ToolError` if there is none."""
        ...


class TokenIdentity:
    """HTTP: the customer, conversation and turn from the request's verified turn token."""

    def scope(self) -> CustomerScope:
        token = get_access_token()
        if token is None or token.subject is None or token.claims is None:
            # The bearer middleware rejects such requests first; this guards a misconfiguration.
            raise ToolError("[UNAUTHENTICATED] This request has no verified customer.")
        return CustomerScope(
            customer_id=uuid.UUID(token.subject),
            conversation_id=uuid.UUID(token.claims["cid"]),
            turn_seq=int(token.claims["tseq"]),
        )


@dataclass
class BoundIdentity:
    """stdio and tests: one customer for the life of the server."""

    customer: CustomerScope

    def scope(self) -> CustomerScope:
        return self.customer


class TurnTokenVerifier:
    """The MCP SDK's `TokenVerifier` for our per-turn tokens."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = turn_tokens.verify(token, self._secret)
        except turn_tokens.InvalidTurnTokenError as error:
            logger.info("Rejected a turn token: %s", error)
            return None
        return AccessToken(
            token=token,
            client_id=turn_tokens.ISSUER,
            scopes=[],
            expires_at=claims.expires_at,
            subject=str(claims.customer_id),
            claims={"cid": str(claims.conversation_id), "tseq": claims.turn_seq},
        )


class UnknownCustomerError(LookupError):
    pass


async def bind_customer(session: AsyncSession, reference: str) -> CustomerScope:
    """The scope for a stdio server: `reference` is a customer's email address or ID."""
    reference = reference.strip()
    try:
        condition = Customer.id == uuid.UUID(reference)
    except ValueError:
        condition = func.lower(Customer.email) == reference.lower()
    customer_id = await session.scalar(select(Customer.id).where(condition))
    if customer_id is None:
        raise UnknownCustomerError(
            f"No customer {reference!r}. Set RELAY_MCP_CUSTOMER to the email of a seeded "
            "customer, for example dana.whitfield@example.com (run `make seed` first)."
        )
    return CustomerScope(customer_id=customer_id)  # no conversation and no turns over stdio
