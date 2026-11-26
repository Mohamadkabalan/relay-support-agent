"""Rate limits (§11, §15): fixed one-minute and one-day windows counted in Redis.

A fixed window is the simplest limit that is exact across API replicas: one INCR per request.
Its known weakness, up to twice the limit across a window boundary, doesn't matter at these
sizes.
"""

import time

from redis.asyncio import Redis


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "relay:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def hit(self, name: str, subject: str, limit: int, window_s: int) -> int | None:
        """Count one request. Returns None if it is allowed, else the seconds until it would be."""
        now = int(time.time())
        window = now // window_s
        key = f"{self.prefix}rate:{name}:{subject}:{window}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, window_s + 5)
        if count > limit:
            return (window + 1) * window_s - now
        return None
