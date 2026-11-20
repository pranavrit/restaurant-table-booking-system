"""Process 4.0 Process Cancellation and Attendance (FR-006, FR-034, FR-035, FR-038 to FR-042)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from ..db import transaction
from ..domain import Audit, LinkPurpose, Status, minutes
from ..repositories import AllocationRepository, AuditLog, Booking, BookingRepository, SetupRepository
from .common import Actor, Result
from .notification import NotificationService

INVALID_LINK = "This link is not valid any more. If you need help, please contact the restaurant."


class CancellationService:
    def __init__(self, conn: sqlite3.Connection, rt):
        self.conn = conn
        self.rt = rt
        self.setup_repo = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.allocs = AllocationRepository(conn)
        self.audit = AuditLog(conn)
        self.notifier = NotificationService(conn, rt)

    # FR-042: unknown, malformed, wrong-purpose or ended links all give the same answer
    def resolve_link(self, token: str, purpose: str, now: datetime) -> Booking | None:
        if not token or len(token) > 80:
            return None
        found = self.bookings.find_by_token(token)
        if found is None:
            return None
        booking, link_purpose = found
        if link_purpose != purpose or booking.status != Status.CONFIRMED or booking.start_dt <= now:
            return None
        return booking

    def _cancel(self, b: Booking, actor: Actor, late: bool, now: datetime) -> list[int]:
        self.bookings.set_status(b.id, Status.CANCELLED, now, late=late)
        self.allocs.release_for_booking(b.id)  # the table is free again at once
        self.audit.record(now, actor.name, Audit.CANCELLED, b.reference, "late" if late else "")
        return self.notifier.on_cancelled(b, now)

    def cancel_by_link(self, token: str) -> Result:
        ids: list[int] = []
        with transaction(self.conn):
            setup = self.setup_repo.load()
            now = self.rt.clock.now(setup.tz)
            b = self.resolve_link(token, LinkPurpose.CANCEL, now)
            if b is None:
                return Result.fail(INVALID_LINK, reason="invalid")
            cutoff = b.start_dt - minutes(setup.values["cutoff_minutes"])
            if now > cutoff:  # FR-039: change nothing after the cutoff
                return Result.fail(
                    "It is too late to cancel online. Please phone the restaurant on "
                    f"{setup.info.get('contact_phone', '')}; late cancellations are made by phone.",
                    reason="late", phone=setup.info.get("contact_phone", ""))
            ids = self._cancel(b, Actor(Actor.GUEST), False, now)
        self.notifier.dispatch(ids)
        return Result.success("Your booking is cancelled.", booking=b)

    def cancel_by_staff(self, reference: str, actor: Actor) -> Result:
        ids: list[int] = []
        with transaction(self.conn):
            setup = self.setup_repo.load()
            now = self.rt.clock.now(setup.tz)
            b = self.bookings.by_reference(reference)
            if b is None:
                return Result.fail("Booking not found.")
            if b.status != Status.CONFIRMED:
                return Result.fail(f"A {b.status} booking cannot be cancelled.")
            late = now > b.start_dt - minutes(setup.values["cutoff_minutes"])  # FR-040
            ids = self._cancel(b, actor, late, now)
        self.notifier.dispatch(ids)
        return Result.success("Booking cancelled" + (" (recorded as a late cancellation)." if late else "."),
                              late=late)

    def confirm_attendance(self, token: str) -> Result:
        with transaction(self.conn):
            setup = self.setup_repo.load()
            now = self.rt.clock.now(setup.tz)
            b = self.resolve_link(token, LinkPurpose.ATTEND, now)
            if b is None:
                return Result.fail(INVALID_LINK, reason="invalid")
            self.bookings.mark_attendance(b.id, now)
        return Result.success("Thank you. We will see you then.", booking=b)
