"""One clock for all time rules (IF-006). Times are naive and held in the restaurant time zone."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, fixed=None):
        # `fixed` is a datetime or a zero-argument callable; used by tests and demos.
        self.fixed = fixed

    def now(self, tz_name: str | None = None) -> datetime:
        if self.fixed is not None:
            return self.fixed() if callable(self.fixed) else self.fixed
        try:
            zone = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        except Exception:
            zone = ZoneInfo("UTC")
        return datetime.now(zone).replace(tzinfo=None, microsecond=0)
