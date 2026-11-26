import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from tests.conftest import TEST_JWT_SECRET

from relay import turn_tokens
from relay.turn_tokens import InvalidTurnTokenError

CUSTOMER, CONVERSATION = uuid.uuid7(), uuid.uuid7()


def mint(now: datetime | None = None, secret: str = TEST_JWT_SECRET) -> str:
    return turn_tokens.mint(
        secret,
        customer_id=CUSTOMER,
        conversation_id=CONVERSATION,
        turn_seq=3,
        now=now or datetime.now(UTC),
    )


def forge(**changes: object) -> str:
    claims = jwt.decode(mint(), TEST_JWT_SECRET, algorithms=["HS256"], audience="relay-mcp")
    claims.update(changes)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, TEST_JWT_SECRET, "HS256")


def test_a_minted_token_verifies_to_its_scope() -> None:
    claims = turn_tokens.verify(mint(), TEST_JWT_SECRET)

    scope = claims.scope()
    assert (scope.customer_id, scope.conversation_id, scope.turn_seq) == (
        CUSTOMER,
        CONVERSATION,
        3,
    )


def test_tokens_expire_after_five_minutes_with_a_little_leeway() -> None:
    issued = datetime.now(UTC) - timedelta(minutes=5)
    turn_tokens.verify(mint(issued), TEST_JWT_SECRET)  # inside the 5-second leeway
    with pytest.raises(InvalidTurnTokenError, match="expired"):
        turn_tokens.verify(mint(issued - timedelta(seconds=10)), TEST_JWT_SECRET)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (lambda: mint(secret="another-secret-of-the-required-length-0"), "Signature"),
        (lambda: forge(aud="relay-api"), "(?i)audience"),
        (lambda: forge(iss="relay-mcp"), "issuer"),
        (lambda: forge(cid=None), "cid"),
        (lambda: forge(tseq=None), "tseq"),
        (lambda: forge(tseq=0), "tseq"),
        (lambda: forge(tseq="2"), "tseq"),
        (lambda: forge(tseq=True), "tseq"),
        (lambda: forge(sub="dana.whitfield@example.com"), "malformed"),
        (lambda: jwt.encode({"sub": str(CUSTOMER)}, key="", algorithm="none"), "alg value"),
    ],
)
def test_rejects_forged_or_incomplete_tokens(token: object, reason: str) -> None:
    with pytest.raises(InvalidTurnTokenError, match=reason):
        turn_tokens.verify(token(), TEST_JWT_SECRET)  # type: ignore[operator]
