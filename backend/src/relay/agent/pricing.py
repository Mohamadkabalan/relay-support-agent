"""What a model call costs, in millionths of a US dollar ("micros").

Prices are list prices per million tokens from platform.claude.com/docs/en/about-claude/models
and the Claude Opus 5.5 migration notes, checked 2026-11-26. Cache writes use the 5-minute TTL
price (1.25x input), the only TTL Relay uses. A model without an entry costs `None`: reports show
it as unknown rather than as zero. Claude Haiku 5.5's higher price above 100K-token prompts is
not modeled; Relay's prompts stay far below it.

With prices in dollars per million tokens, `tokens * price` is already in micros.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True, slots=True)
class Price:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


def _price(input_: str, output: str, cache_read: str, cache_write: str) -> Price:
    return Price(Decimal(input_), Decimal(output), Decimal(cache_read), Decimal(cache_write))


PRICES: dict[str, Price] = {
    "claude-opus-5-5": _price("4", "20", "0.20", "5"),
    "claude-opus-5": _price("5", "25", "0.50", "6.25"),  # a server-side fallback target
    "claude-opus-4-8": _price("5", "25", "0.50", "6.25"),  # a server-side fallback target
    "claude-sonnet-5-5": _price("2", "10", "0.10", "2.50"),
    "claude-haiku-5-5": _price("0.10", "0.50", "0.01", "0.125"),
}


def cost_micros(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> int | None:
    price = PRICES.get(model)
    if price is None:
        return None
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    )
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
