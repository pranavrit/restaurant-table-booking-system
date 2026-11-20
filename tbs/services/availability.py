"""Process 2.0 Provide Availability (FR-007, FR-010, FR-011, FR-014)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain import D_FMT, T_FMT, Setup, minutes, to_dt
from ..repositories import AllocationRepository
from .selection import TableSelector


@dataclass
class Availability:
    times: list[str] = field(default_factory=list)
    message: str = ""
    reason: str = ""  # unavailable | large_party | invalid | closed | past | horizon | full | ok
    phone: str = ""


class AvailabilityService:
    def __init__(self, conn: sqlite3.Connection):
        self.allocs = AllocationRepository(conn)

    def search(self, day_str: str, party_raw, setup: Setup, now: datetime) -> Availability:
        if not setup.complete:
            return Availability(message="Online booking is not available yet. Please try again later.",
                                reason="unavailable")
        try:
            party = int(party_raw)
            day = datetime.strptime(day_str, D_FMT).date()
        except (TypeError, ValueError):
            return Availability(message="Enter a date and a party size.", reason="invalid")
        phone = setup.info.get("contact_phone", "")
        limit = setup.values["online_max_party"]
        if party < 1:
            return Availability(message="Party size must be at least 1.", reason="invalid")
        if party > limit:
            return Availability(
                message=f"For parties of more than {limit}, please phone the restaurant on {phone}.",
                reason="large_party", phone=phone)
        if day < now.date():
            return Availability(message="That date has passed.", reason="past")
        if day > (now + timedelta(days=setup.values["horizon_days"])).date():
            return Availability(message=f"We take bookings up to {setup.values['horizon_days']} days ahead.",
                                reason="horizon")
        if day.strftime(D_FMT) in setup.closures:
            return Availability(message="The restaurant is closed on that date.", reason="closed")
        times = self.start_times(setup, day_str, party, now)
        if not times:
            return Availability(message="No tables are free on that date for your party size.", reason="full")
        return Availability(times=times, reason="ok", phone=phone)

    def start_times(self, setup: Setup, day_str: str, party: int, now: datetime, *, online: bool = True,
                    exclude_booking_id: int | None = None) -> list[str]:
        day = datetime.strptime(day_str, D_FMT)
        allocations = self.allocs.day_allocations(day_str, exclude_booking_id)
        slot = setup.values["slot_minutes"]
        duration = minutes(setup.duration_for(party))
        pad = minutes(setup.values["buffer_minutes"])
        earliest = now + minutes(setup.values["lead_minutes"]) if online else now
        seen: set[str] = set()
        for period in setup.periods:
            if not period.runs_on(day.date()):
                continue
            first = to_dt(day_str, period.start)
            offset = (first.hour * 60 + first.minute) % slot
            if offset:
                first += minutes(slot - offset)
            last = to_dt(day_str, period.last_start)
            t = first
            while t <= last:
                if t >= earliest and self._can_seat(setup, party, t, t + duration, pad, allocations):
                    seen.add(t.strftime(T_FMT))
                t += minutes(slot)
        return sorted(seen)

    @staticmethod
    def _can_seat(setup: Setup, party: int, start: datetime, end: datetime, pad: timedelta, allocations) -> bool:
        busy = {tid for tid, s, e in allocations if s < end + pad and e > start - pad}
        free = [t for t in setup.tables if t.id not in busy]
        return TableSelector.select(party, free) is not None

    def alternatives_near(self, setup: Setup, day_str: str, start: str, party: int, now: datetime, *,
                          count: int = 3, online: bool = True, exclude_booking_id: int | None = None) -> list[str]:
        """Up to three nearest available start times on the same date (FR-011)."""
        try:
            wanted = to_dt(day_str, start)
        except ValueError:
            return []
        options = [t for t in self.start_times(setup, day_str, party, now, online=online,
                                               exclude_booking_id=exclude_booking_id) if t != start]
        options.sort(key=lambda t: (abs((to_dt(day_str, t) - wanted).total_seconds()), t))
        return sorted(options[:count])
