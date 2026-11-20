"""Service layer. `Services` wires the repositories and services for one database connection."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..clock import Clock
from ..repositories import SetupRepository
from .account_service import AccountService
from .availability import AvailabilityService
from .booking_service import BookingService
from .cancellation import CancellationService
from .mail import EmailGateway
from .notification import NotificationService
from .outcome_service import OutcomeService
from .report_service import ReportService
from .retention_service import RetentionService
from .setup_service import SetupService


@dataclass
class Runtime:
    """Process-wide settings the services need."""

    secret: str
    base_url: str
    sender: str
    clock: Clock
    gateway: EmailGateway
    db_path: str
    async_email: bool = False
    hourly_limit: int = 0


class Services:
    def __init__(self, conn: sqlite3.Connection, rt: Runtime):
        self.conn, self.rt = conn, rt
        self.setup_repo = SetupRepository(conn)
        self.setup = SetupService(conn, rt.clock)
        self.accounts = AccountService(conn, rt.clock, lambda: self.setup_repo.load().tz)
        self.availability = AvailabilityService(conn)
        self.bookings = BookingService(conn, rt)
        self.cancellations = CancellationService(conn, rt)
        self.notifications = NotificationService(conn, rt)
        self.outcomes = OutcomeService(conn, rt.clock)
        self.reports = ReportService(conn, rt.clock)
        self.retention = RetentionService(conn, rt.clock)

    def load_setup(self):
        return self.setup_repo.load()

    def now(self, setup=None):
        return self.rt.clock.now((setup or self.load_setup()).tz)
