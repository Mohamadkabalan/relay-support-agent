"""The hard daily spend cap (PLAN.md §12), with reservations.

Before each model call the loop reserves that call's worst-case cost: the request priced as if
nothing came from the cache, plus `max_tokens` of output. The reservation succeeds only if the
day's total, reservations included, stays under the cap. After the call the reservation is
settled to the actual cost. A call that failed keeps its reservation: whether it was billed is
unknown, and the cap should err towards stopping.

Days are UTC days, so the demo's "paused until 00:00 UTC" message is true.
"""

import json
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from typing import Protocol

from redis.asyncio import Redis

from relay.agent.pricing import PRICES
from relay.agent.providers.base import ModelRequest
from relay.clock import Clock

# Characters per token in the estimate. Claude averages nearer 4 for English and JSON; 3 keeps
# the estimate on the high side.
CHARS_PER_TOKEN = 3

RESERVE_SCRIPT = """
local total = tonumber(redis.call("GET", KEYS[1]) or "0")
if total + tonumber(ARGV[1]) > tonumber(ARGV[2]) then
    return -1
end
local after = redis.call("INCRBY", KEYS[1], ARGV[1])
redis.call("EXPIRE", KEYS[1], 172800)
return after
"""


class SpendCapReachedError(Exception):
    """The day's spend, reservations included, would pass the cap. No model call was made."""


@dataclass(frozen=True, slots=True)
class Reservation:
    key: str
    micros: int


class SpendGuard(Protocol):
    async def reserve(self, micros: int) -> Reservation:
        """Reserve `micros`, or raise `SpendCapReachedError`."""
        ...

    async def settle(self, reservation: Reservation, actual_micros: int | None) -> None: ...


def worst_case_micros(model: str, request: ModelRequest) -> int:
    """The most this request can cost: every input token at the full input price, plus all of
    `max_tokens` at the output price. A model without a price is priced like the dearest one."""
    price = PRICES.get(model) or max(PRICES.values(), key=lambda p: p.output)
    chars = len(request.system) + sum(len(raw) for raw in request.transcript)
    chars += sum(
        len(tool.description) + len(json.dumps(tool.input_schema)) for tool in request.tools
    )
    input_tokens = chars // CHARS_PER_TOKEN + 1
    total = input_tokens * price.input + request.max_tokens * price.output
    return int(total.to_integral_value()) + 1


class RedisSpend:
    def __init__(self, redis: Redis, cap_usd: float, clock: Clock, prefix: str = "relay:") -> None:
        self.redis = redis
        self.cap_micros = int(Decimal(str(cap_usd)) * 1_000_000)
        self.clock = clock
        self.prefix = prefix

    def key(self) -> str:
        return f"{self.prefix}spend:{self.clock.now().astimezone(UTC):%Y-%m-%d}"

    async def reserve(self, micros: int) -> Reservation:
        key = self.key()
        after = await self.redis.eval(RESERVE_SCRIPT, 1, key, micros, self.cap_micros)
        if int(after) < 0:
            raise SpendCapReachedError(f"reserving {micros} micros would pass the daily cap")
        return Reservation(key, micros)

    async def settle(self, reservation: Reservation, actual_micros: int | None) -> None:
        if actual_micros is None:
            return  # the model has no price: keep the worst case
        await self.redis.incrby(reservation.key, actual_micros - reservation.micros)

    async def spent_today(self) -> int:
        value = await self.redis.get(self.key())
        return int(value or 0)
