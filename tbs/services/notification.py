"""Process 5.0 Send Guest Messages (FR-031 to FR-036, IF-003, IF-004, CON-002)."""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from ..db import connect, transaction
from ..domain import MsgState, MsgType, Setup, LinkPurpose
from ..repositories import Booking, BookingRepository, MessageRepository, SetupRepository, make_token
from .mail import EmailGateway, OutboundMessage

log = logging.getLogger("tbs.notify")
MAX_ATTEMPTS = 4  # the first try plus 3 retries, 5 minutes apart (FR-036)


class NotificationService:
    def __init__(self, conn: sqlite3.Connection, rt):
        self.conn = conn
        self.rt = rt
        self.messages = MessageRepository(conn)
        self.bookings = BookingRepository(conn)
        self.setup = SetupRepository(conn)

    # --- creating message records (inside the caller's transaction)
    def link(self, reference: str, purpose: str) -> str:
        return f"{self.rt.base_url.rstrip('/')}/{purpose}/{make_token(self.rt.secret, reference, purpose)}"

    def on_confirmed(self, booking: Booking, setup: Setup, now: datetime) -> list[int]:
        if not booking.guest_email:  # FR-031.2: no e-mail, no message
            return []
        ids = [self.messages.add(booking.id, MsgType.CONFIRMATION, now, now)]
        self._schedule_reminder(booking, setup, now)
        return ids

    def reschedule_reminder(self, booking: Booking, setup: Setup, now: datetime) -> None:
        self.messages.delete_queued_reminders(booking.id)
        if booking.guest_email:
            self._schedule_reminder(booking, setup, now)

    def _schedule_reminder(self, booking: Booking, setup: Setup, now: datetime) -> None:
        remind_at = booking.start_dt - timedelta(hours=setup.values["reminder_hours"])
        if remind_at >= now:  # FR-033: booked too late for a reminder, so none is created
            self.messages.add(booking.id, MsgType.REMINDER, remind_at, now)

    def on_cancelled(self, booking: Booking, now: datetime) -> list[int]:
        self.messages.delete_queued_reminders(booking.id)
        if not booking.guest_email:
            return []
        return [self.messages.add(booking.id, MsgType.CANCELLATION, now, now)]

    # --- sending (call outside a transaction)
    def dispatch(self, ids: list[int]) -> None:
        """Hand new messages to the e-mail service after the booking has committed (NFR-005)."""
        if not ids:
            return
        if self.rt.async_email:
            threading.Thread(target=_dispatch_job, args=(self.rt, list(ids)), daemon=True).start()
        else:
            for mid in ids:
                self.send_one(mid)

    def process_due(self) -> int:
        now = self.rt.clock.now(self.setup.load().tz)
        count = 0
        for row in self.messages.due(now):
            if self.send_one(row["id"]):
                count += 1
        return count

    def send_one(self, mid: int) -> bool:
        tz = self.setup.load().tz
        now = self.rt.clock.now(tz)
        with transaction(self.conn):
            row = self.messages.get(mid)
            if row is None or row["state"] != MsgState.QUEUED or row["scheduled_for"] > now.strftime("%Y-%m-%d %H:%M:%S"):
                return False
            booking = self.bookings.get(row["booking_id"])
            if booking is None or not booking.guest_email:
                self.messages.delete(mid)
                return False
            if row["type"] == MsgType.REMINDER and booking.status != "Confirmed":
                self.messages.delete(mid)  # FR-033: no reminder for a booking that is not Confirmed
                return False
            limit = self.rt.hourly_limit
            if limit and self.messages.sent_since(now - timedelta(hours=1)) >= limit:
                return False  # CON-002: wait for the limit to allow it, oldest first
            if not self.messages.claim(mid, now):
                return False
            setup = self.setup.load()
            outbound = self._build(booking, row["type"], setup)
            attempts = row["attempts"] + 1
        state = self.rt.gateway.send(outbound)
        done = self.rt.clock.now(tz)
        with transaction(self.conn):
            if state == MsgState.SENT:
                self.messages.set_state(mid, MsgState.SENT, done)
            elif attempts >= MAX_ATTEMPTS:
                self.messages.set_state(mid, MsgState.FAILED, done)
        return state == MsgState.SENT

    # --- message text (plain text, English, 24-hour times; NFR-024)
    def _build(self, b: Booking, mtype: str, setup: Setup) -> OutboundMessage:
        name = setup.info.get("restaurant_name", "the restaurant")
        when = f"{b.start_dt.strftime('%A %d %B %Y')} at {b.start_time}"
        details = (f"Booking reference: {b.reference}\nRestaurant: {name}\nWhen: {when}\n"
                   f"Party size: {b.party_size}\n")
        rules = (f"Grace period: we hold your table for {setup.values['grace_minutes']} minutes after the start time.\n"
                 f"Cancellation cutoff: you can cancel online until {setup.values['cutoff_minutes']} minutes "
                 f"before the start time; after that please phone {setup.info.get('contact_phone', '')}.\n")
        if mtype == MsgType.CONFIRMATION:
            subject = f"Your booking at {name} is confirmed ({b.reference})"
            body = (f"Hello {b.guest_name},\n\nYour table is booked.\n\n{details}\n{rules}\n"
                    f"Cancel: {self.link(b.reference, LinkPurpose.CANCEL)}\n"
                    f"Change: {self.link(b.reference, LinkPurpose.CHANGE)}\n")
        elif mtype == MsgType.REMINDER:
            subject = f"Reminder: your table at {name} ({b.reference})"
            body = (f"Hello {b.guest_name},\n\nThis is a reminder of your booking.\n\n{details}\n{rules}\n"
                    f"Confirm you are coming: {self.link(b.reference, LinkPurpose.ATTEND)}\n"
                    f"Cancel: {self.link(b.reference, LinkPurpose.CANCEL)}\n")
        else:
            subject = f"Your booking at {name} is cancelled ({b.reference})"
            body = f"Hello {b.guest_name},\n\nYour booking has been cancelled.\n\n{details}"
        return OutboundMessage(to=b.guest_email, subject=subject, body=body, sender=self.rt.sender)


def _dispatch_job(rt, ids: list[int]) -> None:
    conn = connect(rt.db_path)
    try:
        service = NotificationService(conn, rt)
        for mid in ids:
            service.send_one(mid)
    except Exception:  # background job: the message stays queued and the worker retries it
        log.exception("background send failed")
    finally:
        conn.close()
