"""Browser sessions (§11): an opaque ID in an HttpOnly cookie, the data in Redis under a TTL.

The cookie holds a random token. Redis stores the session under an HMAC of that token, so
someone who reads Redis can't use what they find there. A login always issues a new token
(no session fixation), and every request that loads a session renews its TTL.

A session can carry a customer (from the persona login) and an operator (from the operator or
demo-operator login) at once: the demo's chat widget and console share one browser. Customer
routes read only the customer, operator routes only the operator.
"""

import hashlib
import hmac
import secrets
import uuid

from pydantic import BaseModel
from redis.asyncio import Redis


class SessionData(BaseModel):
    visitor_id: str  # one browser; demo copies of the personas belong to it
    customer_id: uuid.UUID | None = None
    persona: str | None = None
    operator_id: uuid.UUID | None = None
    operator_name: str | None = None
    # The demo operator sees only this visitor's customers (D4); the real operator sees all.
    operator_scoped: bool = False


class SessionStore:
    def __init__(self, redis: Redis, secret: str, ttl_s: int, prefix: str = "relay:") -> None:
        self.redis = redis
        self.secret = secret.encode()
        self.ttl_s = ttl_s
        self.prefix = prefix

    def _key(self, token: str) -> str:
        digest = hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()
        return f"{self.prefix}session:{digest}"

    async def create(self, data: SessionData) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.set(self._key(token), data.model_dump_json(), ex=self.ttl_s)
        return token

    async def load(self, token: str) -> SessionData | None:
        raw = await self.redis.getex(self._key(token), ex=self.ttl_s)
        return SessionData.model_validate_json(raw) if raw else None

    async def delete(self, token: str) -> None:
        await self.redis.delete(self._key(token))
