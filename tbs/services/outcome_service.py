"""Process 6.0 Record Outcomes (FR-043 to FR-048, FR-050)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..db import transaction
from ..domain import Audit, D_FMT, Status, T_FMT, minutes
from ..repositories import (
    AllocationRepository,
    AuditLog,
    Booking,
    BookingRepository,
    ConflictError,
    MessageRepository,
    SetupRepository,
)
from .common import Actor, Result


@dataclass
class DayRow:
    kind: str  # booking | walkin
    start: str
    party: int
    tables: str
    booking: Booking | None = None
    walkin_id: int | None = None
    message_failed: bool = False

    @property
    def no_email_mark(self) -> bool:  # FR-037
        return bool(self.booking and self.booking.status == Status.CONFIRMED and not self.booking.guest_email
                    and not self.booking.personal_removed)


class OutcomeService:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock
        self.setup_repo = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.allocs = AllocationRepository(conn)
        self.messages = MessageRepository(conn)
        self.audit = AuditLog(conn)

    def day_list(self, day: str) -> list[DayRow]:
        bookings = self.bookings.for_date(day)
        failed = self.messages.failed_booking_ids([b.id for b in bookings])
        rows = [DayRow("booking", b.start_time, b.party_size, self.allocs.tables_label_for_booking(b.id), booking=b,
                       message_failed=b.id in failed) for b in bookings]
        for w in self.bookings.walkins_for_date(day):
            rows.append(DayRow("walkin", w["arrival"][11:16], w["party_size"], w["tables"] or "", walkin_id=w["id"]))
        rows.sort(key=lambda r: (r.start, r.kind))
        return rows

    def outcome_missing(self) -> list[Booking]:
        """FR-047: still Confirmed after start plus the grace period."""
        setup = self.setup_repo.load()
        now = self.clock.now(setup.tz)
        grace = minutes(setup.values["grace_minutes"])
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status='Confirmed' AND booking_date <= ? ORDER BY booking_date, start_time",
            (now.strftime(D_FMT),)).fetchall()
        return [b for b in map(Booking.from_row, rows) if b.start_dt + grace < now]

    # ---- one-step outcome marks
    def _load(self, reference: str):
        setup = self.setup_repo.load()
        now = self.clock.now(setup.tz)
        return setup, now, self.bookings.by_reference(reference)

    def mark_arrived(self, reference: str, actor: Actor) -> Result:
        with transaction(self.conn):
            setup, now, b = self._load(reference)
            if b is None:
                return Result.fail("Booking not found.")
            if b.status != Status.CONFIRMED:
                return Result.fail(f"A {b.status} booking cannot be marked Arrived.")
            self.bookings.set_status(b.id, Status.ARRIVED, now, arrived_at=now)
            self.audit.record(now, actor.name, Audit.STATUS, b.reference, "Confirmed -> Arrived")
        return Result.success("Marked Arrived.")

    def mark_seated(self, reference: str, actor: Actor) -> Result:
        with transaction(self.conn):
            setup, now, b = self._load(reference)
            if b is None:
                return Result.fail("Booking not found.")
            if b.status not in (Status.CONFIRMED, Status.ARRIVED):
                return Result.fail(f"A {b.status} booking cannot be marked Seated.")
            self._seat(b, now)
            self.bookings.set_status(b.id, Status.SEATED, now)
            self.audit.record(now, actor.name, Audit.STATUS, b.reference, f"{b.status} -> Seated")
        return Result.success("Marked Seated.")

    def _seat(self, b: Booking, now: datetime) -> None:
        if not self.bookings.visit_exists(b.id):
            arrival = datetime.strptime(b.arrived_at, "%Y-%m-%d %H:%M:%S") if b.arrived_at else now
            tables = [a.table_id for a in self.allocs.current_set(b.id)]
            self.bookings.create_visit(b.id, arrival, b.party_size, tables)

    def mark_no_show(self, reference: str, actor: Actor) -> Result:
        with transaction(self.conn):
            setup, now, b = self._load(reference)
            if b is None:
                return Result.fail("Booking not found.")
            if b.status != Status.CONFIRMED:
                return Result.fail(f"A {b.status} booking cannot be marked No-show.")
            earliest = b.start_dt + minutes(setup.values["grace_minutes"])
            if now < earliest:  # FR-046
                return Result.fail("Too early for a no-show. The grace period ends at "
                                   f"{earliest.strftime(T_FMT)}.")
            self.bookings.set_status(b.id, Status.NO_SHOW, now)
            self.allocs.release_for_booking(b.id)
            self.audit.record(now, actor.name, Audit.STATUS, b.reference, "Confirmed -> No-show")
        return Result.success("Marked No-show. The table is free again.")

    # ---- FR-048
    def correct(self, reference: str, new_status: str, actor: Actor) -> Result:
        if new_status not in Status.ALL:
            return Result.fail("Choose a valid status.")
        try:
            with transaction(self.conn):
                setup, now, b = self._load(reference)
                if b is None:
                    return Result.fail("Booking not found.")
                today = now.date()
                if b.start_dt.date() not in (today, today - timedelta(days=1)):
                    return Result.fail("Only bookings of today or yesterday can be corrected.")
                if new_status == b.status:
                    return Result.fail("The booking already has that status.")
                old = b.status
                if new_status in Status.ACTIVE and old not in Status.ACTIVE:
                    self.allocs.reactivate_for_booking(b.id, setup.values["buffer_minutes"])
                if new_status in (Status.CANCELLED, Status.NO_SHOW):
                    self.allocs.release_for_booking(b.id)
                if new_status == Status.SEATED:
                    self._seat(b, now)
                self.bookings.set_status(b.id, new_status, now, late=False if old == Status.CANCELLED else None)
                self.audit.record(now, actor.name, Audit.CORRECTED, b.reference, f"{old} -> {new_status}")
        except ConflictError as exc:
            c = exc.conflict
            return Result.fail(f"Not corrected: table {c.label} is booked from {c.start.strftime(T_FMT)} to "
                               f"{c.end.strftime(T_FMT)}.")
        return Result.success(f"Status corrected to {new_status}.")
