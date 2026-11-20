"""Process 7.0 Produce Reports (FR-051, FR-052, FR-054, FR-055, FR-056). No ranking of people (BR-06)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain import Audit, D_FMT, Status
from ..repositories import AuditLog, BookingRepository, SetupRepository
from .common import Result

MAX_RANGE_DAYS = 366


@dataclass
class Report:
    first: str
    last: str
    counts: dict[str, int] = field(default_factory=dict)
    refused_conflicts: int = 0
    no_show_rate: str = "no data"
    no_show_detail: str = ""
    outcome_share: str = "no data"


class ReportService:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.clock = clock
        self.setup_repo = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.audit = AuditLog(conn)

    @staticmethod
    def _range(first: str, last: str):
        try:
            a, b = datetime.strptime(first, D_FMT), datetime.strptime(last, D_FMT)
        except ValueError:
            return None, "Enter both dates as YYYY-MM-DD."
        if b < a:
            return None, "The end date is before the start date."
        if (b - a).days + 1 > MAX_RANGE_DAYS:
            return None, f"The range is longer than {MAX_RANGE_DAYS} days."
        return (a, b), ""

    def summary(self, first: str, last: str) -> Result:
        span, error = self._range(first, last)
        if span is None:
            return Result.fail(error)
        a, b = span
        setup = self.setup_repo.load()
        now = self.clock.now(setup.tz)
        rows = self.bookings.in_range(first, last)
        counts = {
            "Seated": 0, "No-show": 0, "Cancelled before the cutoff": 0, "Cancelled late": 0,
            "Arrived, not yet seated": 0, "Confirmed without outcome": 0,
        }
        for r in rows:
            if r.status == Status.SEATED:
                counts["Seated"] += 1
            elif r.status == Status.NO_SHOW:
                counts["No-show"] += 1
            elif r.status == Status.CANCELLED:
                counts["Cancelled late" if r.late_cancel else "Cancelled before the cutoff"] += 1
            elif r.status == Status.ARRIVED:
                counts["Arrived, not yet seated"] += 1
            else:
                counts["Confirmed without outcome"] += 1
        report = Report(first, last, counts)
        report.refused_conflicts = len(self.audit.between(a, b + timedelta(days=1), Audit.REFUSED))
        reached = [r for r in rows if r.start_dt <= now and not (r.status == Status.CANCELLED and not r.late_cancel)]
        if reached:
            n = sum(1 for r in reached if r.status == Status.NO_SHOW)
            report.no_show_rate = f"{100 * n / len(reached):.1f} percent"
            report.no_show_detail = f"{n} no-show out of {len(reached)} qualifying bookings"
        past = [r for r in rows if r.start_dt < now]
        if past:
            share = 100 * sum(1 for r in past if r.status != Status.CONFIRMED) / len(past)
            report.outcome_share = f"{share:.0f} percent"
        return Result.success(report=report)

    def exception_log(self, first: str, last: str) -> Result:
        span, error = self._range(first, last)
        if span is None:
            return Result.fail(error)
        a, b = span
        rows = self.audit.between(a, b + timedelta(days=1), Audit.EXCEPTION)
        return Result.success(entries=[
            {"date": r["at"][:10], "actor": r["actor"], "reason": r["detail"], "reference": r["reference"]}
            for r in rows])
