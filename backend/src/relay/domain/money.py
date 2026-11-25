"""Money is integer cents in USD. Prices are tax-inclusive."""


def format_cents(cents: int) -> str:
    """34999 -> "$349.99", -500 -> "-$5.00"."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"
