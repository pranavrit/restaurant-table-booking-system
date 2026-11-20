"""Process 1.0 Maintain Restaurant Setup (FR-021 to FR-030)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from ..db import transaction
from ..domain import (
    D_FMT,
    T_FMT,
    Status,
    validate_restaurant_info,
    validate_settings,
)
from ..repositories import AllocationRepository, BookingRepository, SetupRepository
from .common import Result


def _hhmm(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), T_FMT).strftime(T_FMT)
    except ValueError:
        return None


class SetupService:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock
        self.setup = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.allocs = AllocationRepository(conn)

    def _now(self) -> datetime:
        return self.clock.now(self.setup.load().tz)

    # FR-024, FR-025, FR-026, FR-023, FR-030: one submission is saved whole or not at all
    def save_settings(self, raw: dict[str, str]) -> Result:
        values, errors = validate_settings(raw)
        if errors:
            return Result.fail("Nothing was saved. Fix the values marked below.", errors)
        with transaction(self.conn):
            self.setup.save_settings(values)
        return Result.success("Settings saved. They apply to bookings made or changed from now on.")

    def save_info(self, raw: dict[str, str]) -> Result:
        info, errors = validate_restaurant_info(raw)
        if errors:
            return Result.fail("Nothing was saved. Fix the values marked below.", errors)
        with transaction(self.conn):
            self.setup.save_settings(info)
        return Result.success("Restaurant details saved.")

    # FR-021
    def save_table(self, raw: dict, table_id: int | None = None) -> Result:
        errors: dict[str, str] = {}
        label = (raw.get("label") or "").strip()
        if not 1 <= len(label) <= 20:
            errors["label"] = "Label must be 1 to 20 characters."
        try:
            low, high = int(raw.get("min_seats", "")), int(raw.get("max_seats", ""))
        except ValueError:
            errors["max_seats"] = "Seat numbers must be whole numbers."
            low = high = 0
        if not errors:
            if low < 1:
                errors["min_seats"] = "Minimum seats must be at least 1."
            elif high < low:
                errors["max_seats"] = "Maximum seats must not be below the minimum."
            elif high > 50:
                errors["max_seats"] = "Maximum seats must be 50 or fewer."
        with transaction(self.conn):
            setup = self.setup.load()
            if label and any(t.label.lower() == label.lower() and t.id != table_id for t in setup.tables):
                errors["label"] = f"A table labelled {label} already exists."
            others: set[int] = set()
            for value in raw.get("combinable", []) or []:
                try:
                    other = int(value)
                except ValueError:
                    continue
                if setup.table(other) is not None and other != table_id:
                    others.add(other)
            if errors:
                return Result.fail("Nothing was saved. Fix the values marked below.", errors)
            if table_id is None:
                table_id = self.setup.insert_table(label, low, high)
            else:
                self.setup.update_table(table_id, label, low, high)
            self.setup.set_combinable(table_id, others)
        return Result.success(f"Table {label} saved.")

    # FR-029
    def remove_table(self, table_id: int) -> Result:
        with transaction(self.conn):
            now = self._now()
            future = self.allocs.future_for_table(table_id, now)
            if future:
                refs = sorted({self.bookings.get(a.booking_id).reference for a in future if a.booking_id})
                walkins = any(a.visit_id for a in future)
                detail = ", ".join(refs) + (" and a walk-in" if walkins and refs else "walk-in" if walkins else "")
                return Result.fail(f"The table is in use by future bookings, so it was not removed: {detail}.",
                                   affected=refs)
            self.setup.delete_table(table_id)
        return Result.success("Table removed.")

    # FR-022
    def save_period(self, raw: dict, period_id: int | None = None) -> Result:
        errors: dict[str, str] = {}
        name = (raw.get("name") or "").strip()
        if not 1 <= len(name) <= 60:
            errors["name"] = "Name must be 1 to 60 characters."
        days = set()
        for d in raw.get("days", []) or []:
            if str(d) in {"0", "1", "2", "3", "4", "5", "6"}:
                days.add(int(d))
        if not days:
            errors["days"] = "Choose at least one day."
        start, end, last = _hhmm(raw.get("start", "")), _hhmm(raw.get("end", "")), _hhmm(raw.get("last_start", ""))
        for key, val in (("start", start), ("end", end), ("last_start", last)):
            if val is None:
                errors[key] = "Enter a time such as 18:00."
        if not errors:
            if end <= start:
                errors["end"] = "End time must be after the start time."
            elif not start <= last <= end:
                errors["last_start"] = "Last bookable start must be between the start and the end time."
        if errors:
            return Result.fail("Nothing was saved. Fix the values marked below.", errors)
        with transaction(self.conn):
            if period_id is None:
                self.setup.insert_period(name, days, start, end, last)
            else:
                self.setup.update_period(period_id, name, days, start, end, last)
        return Result.success(f"Service period {name} saved.")

    def remove_period(self, period_id: int) -> Result:
        with transaction(self.conn):
            setup = self.setup.load()
            now = self.clock.now(setup.tz)
            period = next((p for p in setup.periods if p.id == period_id), None)
            if period is None:
                return Result.fail("That service period does not exist.")
            affected = []
            for b in self.bookings.active_from(now.strftime(D_FMT)):
                others = [p for p in setup.periods if p.id != period_id and p.runs_on(b.start_dt.date())
                          and p.accepts(b.start_time)]
                if period.runs_on(b.start_dt.date()) and period.accepts(b.start_time) and not others:
                    affected.append(b.reference)
            if affected:
                return Result.fail("The period has future bookings, so it was not removed: "
                                   + ", ".join(sorted(affected)) + ".", affected=affected)
            self.setup.delete_period(period_id)
        return Result.success("Service period removed.")

    # FR-027
    def add_closure(self, day: str) -> Result:
        try:
            parsed = datetime.strptime(day.strip(), D_FMT)
        except ValueError:
            return Result.fail("Enter the date as YYYY-MM-DD.", {"day": "Enter the date as YYYY-MM-DD."})
        key = parsed.strftime(D_FMT)
        with transaction(self.conn):
            self.setup.add_closure(key)
            existing = [b for b in self.bookings.for_date(key) if b.status == Status.CONFIRMED]
        note = ""
        if existing:
            note = f" {len(existing)} confirmed booking(s) on that date were kept; contact those guests."
        return Result.success(f"{key} marked closed.{note}", existing=len(existing))

    def remove_closure(self, day: str) -> Result:
        with transaction(self.conn):
            self.setup.remove_closure(day)
        return Result.success("Closure removed.")
