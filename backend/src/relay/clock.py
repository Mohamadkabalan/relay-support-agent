"""Time is an input.

Nothing in the domain calls `datetime.now()`. Services receive a `Clock`, so tests and evals can
freeze time (the eval clock is 2026-09-15 15:00 America/New_York) and policy boundaries such as
"day 30 versus day 31" are reproducible.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that only moves when told to."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._at = at


def local_date(moment: datetime, tz: ZoneInfo) -> date:
    """The calendar date of `moment` in the store time zone.

    Business dates (return windows, late deliveries, days without a scan) are counted in the store
    time zone, so a package delivered at 23:30 in New York counts as delivered that day, even though
    it is already the next day in UTC.
    """
    if moment.tzinfo is None:
        raise ValueError("local_date needs a timezone-aware datetime")
    return moment.astimezone(tz).date()
