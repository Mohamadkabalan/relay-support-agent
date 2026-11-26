"""The single-flight lock: at most one turn per conversation at a time (ADR-0007).

`hold` yields whether the lock was taken; it never waits. A caller that doesn't get it leaves the
work in the inbox, where the running turn's next check finds it (the Phase 5 scheduler).

- `RedisLocks` works across API replicas: `SET key token NX PX ttl`, released only by the holder
  (a compare-and-delete script). The TTL outlives the turn deadline, so a crashed process can't
  hold a conversation forever.
- `LocalLocks` is the in-process equivalent for the CLI, tests and eval trials.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from redis.asyncio import Redis

RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class Locks(Protocol):
    def hold(self, key: str, ttl_s: float) -> AbstractAsyncContextManager[bool]: ...


class LocalLocks:
    def __init__(self) -> None:
        self._held: set[str] = set()

    @asynccontextmanager
    async def hold(self, key: str, ttl_s: float) -> AsyncIterator[bool]:
        if key in self._held:
            yield False
            return
        self._held.add(key)
        try:
            yield True
        finally:
            self._held.discard(key)


class RedisLocks:
    def __init__(self, redis: Redis, prefix: str = "relay:lock:") -> None:
        self.redis = redis
        self.prefix = prefix

    @asynccontextmanager
    async def hold(self, key: str, ttl_s: float) -> AsyncIterator[bool]:
        name = self.prefix + key
        token = secrets.token_hex(16)
        acquired = await self.redis.set(name, token, nx=True, px=int(ttl_s * 1000))
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            # Shielded so a cancelled turn still releases its lock.
            await asyncio.shield(self.redis.eval(RELEASE_SCRIPT, 1, name, token))
