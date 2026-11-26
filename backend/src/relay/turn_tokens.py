"""Per-turn tokens: who the agent is acting for, during one turn (PLAN.md §6.1, ADR-0003).

Before each turn the API mints a short-lived JWT naming the customer, the conversation and the turn
number, and the agent sends it to the MCP server as a bearer token. The MCP server verifies it and
builds the `CustomerScope` every tool runs under. Tool arguments never carry identity, so the model
cannot act for anyone else, whatever it is told.

HS256 with a secret that only the API and the MCP server hold (`RELAY_MCP_JWT_SECRET`).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt

from relay.domain.scope import CustomerScope

ISSUER = "relay-api"
AUDIENCE = "relay-mcp"
ALGORITHM = "HS256"
DEFAULT_TTL = timedelta(minutes=5)
LEEWAY_SECONDS = 5  # clock skew between the API and MCP containers
REQUIRED_CLAIMS = ("iss", "aud", "sub", "iat", "exp", "cid", "tseq")


class InvalidTurnTokenError(Exception):
    """The token is malformed, forged, expired, or meant for someone else."""


@dataclass(frozen=True, slots=True)
class TurnClaims:
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    turn_seq: int
    expires_at: int  # Unix seconds

    def scope(self) -> CustomerScope:
        return CustomerScope(
            customer_id=self.customer_id,
            conversation_id=self.conversation_id,
            turn_seq=self.turn_seq,
        )


def mint(
    secret: str,
    *,
    customer_id: uuid.UUID,
    conversation_id: uuid.UUID,
    turn_seq: int,
    now: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(customer_id),
        "cid": str(conversation_id),
        "tseq": turn_seq,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify(token: str, secret: str) -> TurnClaims:
    """Check the signature, issuer, audience and expiry, and read the claims."""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS)},
        )
        turn_seq = claims["tseq"]
        if type(turn_seq) is not int or turn_seq < 1:
            raise InvalidTurnTokenError("tseq must be a positive integer")
        return TurnClaims(
            customer_id=uuid.UUID(claims["sub"]),
            conversation_id=uuid.UUID(claims["cid"]),
            turn_seq=turn_seq,
            expires_at=int(claims["exp"]),
        )
    except jwt.PyJWTError as error:
        raise InvalidTurnTokenError(str(error)) from error
    except (ValueError, TypeError, AttributeError) as error:  # sub or cid is not a UUID
        raise InvalidTurnTokenError(f"malformed claims: {error}") from error
