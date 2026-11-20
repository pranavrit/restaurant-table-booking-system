"""Process 8.0, data part: retention and deletion of guest data (FR-061 to FR-064, CON-001)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..db import transaction
from ..domain import Audit, D_FMT, Status, minutes
from ..repositories import AllocationRepository, AuditLog, BookingRepository, SetupRepository
from .common import Actor, Result

AUDIT_KEEP_DAYS = 365  # DR-008: 12 months


class RetentionService:
    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock
        self.setup_repo = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.allocs = AllocationRepository(conn)
        self.audit = AuditLog(conn)

    def remove_expired(self) -> int:
        """FR-062: runs daily. Removes personal data once the retention period has passed."""
        with transaction(self.conn):
            setup = self.setup_repo.load()
            now = self.clock.now(setup.tz)
            cutoff_day = (now - timedelta(days=setup.values["retention_days"])).strftime(D_FMT)
            expired = self.bookings.expired_personal(cutoff_day)
            for b in expired:
                self.bookings.remove_personal(b.id)
            self.audit.purge_older_than(now - timedelta(days=AUDIT_KEEP_DAYS))
            self.setup_repo.save_settings({"last_maintenance": now.strftime(D_FMT)})
            return len(expired)

    def maintenance_due(self) -> bool:
        setup = self.setup_repo.load()
        today = self.clock.now(setup.tz).strftime(D_FMT)
        return self.setup_repo.get_meta("last_maintenance") != today

    def delete_for_email(self, email: str, actor: Actor, confirm_cancel: bool = False) -> Result:
        """FR-063 and FR-064."""
        email = (email or "").strip().lower()
        if not email:
            return Result.fail("Enter an e-mail address.", {"email": "Enter an e-mail address."})
        with transaction(self.conn):
            setup = self.setup_repo.load()
            now = self.clock.now(setup.tz)
            found = self.bookings.by_email(email)
            future = [b for b in found if b.status == Status.CONFIRMED and b.start_dt > now]
            if future and not confirm_cancel:
                return Result.fail(
                    f"{len(future)} confirmed future booking(s) use this address. Deleting the data will "
                    "cancel them first.", needs_confirmation=True, future=len(future))
            for b in future:
                late = now > b.start_dt - minutes(setup.values["cutoff_minutes"])
                self.bookings.set_status(b.id, Status.CANCELLED, now, late=late)
                self.allocs.release_for_booking(b.id)
                self.audit.record(now, actor.name, Audit.CANCELLED, b.reference, "cancelled before data removal")
            for b in found:
                self.bookings.remove_personal(b.id)
            self.audit.record(now, actor.name, Audit.REMOVAL, None, f"{len(found)} booking(s)")
        return Result.success(f"Personal data removed from {len(found)} booking(s).", count=len(found))
