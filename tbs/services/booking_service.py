"""Process 3.0 Process Booking Request (M3 DFD-1: validate, select, reserve, record, decide).

Every create or change runs in one BEGIN IMMEDIATE transaction, so the overlap check and the
write are one atomic step (FR-018). A refusal stores nothing except an audit entry (FR-050).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..db import transaction
from ..domain import (
    Audit,
    Channel,
    D_FMT,
    Setup,
    Status,
    T_FMT,
    fmt_dt,
    minutes,
    normalise_phone,
    to_dt,
    valid_email,
    valid_phone,
    LinkPurpose,
)
from ..repositories import (
    AllocationRepository,
    AuditLog,
    Booking,
    BookingRepository,
    Conflict,
    ConflictError,
    SetupRepository,
    make_token,
)
from .availability import AvailabilityService
from .common import Actor, Decision
from .notification import NotificationService
from .selection import TableSelector, set_fits

ONLINE, STAFF, EXCEPTION = "online", "staff", "exception"
DUPLICATE_WINDOW_SECONDS = 5  # AC-FR-001.2


@dataclass
class BookingRequest:
    date: str = ""
    start: str = ""
    party: str | int = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    step_free: bool = False
    dietary: str = ""
    privacy_ack: bool = False
    channel: str = Channel.WEBSITE
    table_ids: list[int] = field(default_factory=list)
    reason: str = ""


class Refusal(Exception):
    def __init__(self, kind: str, message: str, conflict: Conflict | None = None):
        super().__init__(message)
        self.kind, self.message, self.conflict = kind, message, conflict


def describe_conflict(c: Conflict) -> str:
    return f"table {c.label} is taken from {c.start.strftime(T_FMT)} to {c.end.strftime(T_FMT)} on " \
           f"{c.start.strftime(D_FMT)}"


class BookingService:
    def __init__(self, conn: sqlite3.Connection, rt):
        self.conn = conn
        self.rt = rt
        self.setup_repo = SetupRepository(conn)
        self.bookings = BookingRepository(conn)
        self.allocs = AllocationRepository(conn)
        self.audit = AuditLog(conn)
        self.availability = AvailabilityService(conn)
        self.notifier = NotificationService(conn, rt)

    # ------------------------------------------------------------ validation (3.1)
    def _when_errors(self, errors, setup: Setup, now: datetime, day: str, start: str, mode: str) -> datetime | None:
        try:
            dt = to_dt(day, start)
        except ValueError:
            if not _valid_date(day):
                errors["date"] = "Enter a valid date."
            if not _valid_time(start):
                errors["start"] = "Enter a valid start time such as 19:00."
            return None
        if day in setup.closures:
            errors["date"] = "The restaurant is closed on that date."
            return dt
        if mode == ONLINE:
            if dt < now + minutes(setup.values["lead_minutes"]):
                errors["start"] = "That time is too soon to book online."
            if dt.date() > (now + timedelta(days=setup.values["horizon_days"])).date():
                errors["date"] = f"Bookings open {setup.values['horizon_days']} days ahead at most."
        elif dt < now:
            errors["start"] = "The start time is in the past."
        slot = setup.values["slot_minutes"]
        if "start" not in errors:
            if (dt.hour * 60 + dt.minute) % slot:
                errors["start"] = f"Start times are every {slot} minutes."
            elif setup.period_for(dt.date(), dt.strftime(T_FMT)) is None:
                errors["start"] = "That time is outside the opening hours."
        return dt

    def validate(self, req: BookingRequest, setup: Setup, now: datetime, mode: str) -> dict[str, str]:
        errors: dict[str, str] = {}
        name = req.name.strip()
        if not 1 <= len(name) <= 100:
            errors["name"] = "Enter the guest name (1 to 100 characters)."
        email, phone = req.email.strip(), req.phone.strip()
        if mode == ONLINE:
            if not valid_email(email):
                errors["email"] = "Enter a valid e-mail address."
            if not req.privacy_ack:
                errors["privacy"] = "Please confirm that you have read the privacy notice."
        else:
            if not email and not phone:
                errors["email"] = "Enter an e-mail address or a phone number."
            elif email and not valid_email(email):
                errors["email"] = "Enter a valid e-mail address."
            if phone and not valid_phone(phone):
                errors["phone"] = "Phone numbers have 7 to 15 digits, with an optional leading +."
            if req.channel not in Channel.STAFF:
                errors["channel"] = "Choose how the booking was made."
        if mode == ONLINE and phone and not valid_phone(phone):
            errors["phone"] = "Phone numbers have 7 to 15 digits, with an optional leading +."
        if len(req.dietary) > 300:
            errors["dietary"] = "Keep the note to 300 characters."
        try:
            party = int(str(req.party).strip())
            if not 1 <= party <= 50:
                raise ValueError
            if mode == STAFF and party > setup.values["online_max_party"]:
                errors["party"] = (f"Parties above {setup.values['online_max_party']} need a manager "
                                   "exception booking.")
        except ValueError:
            errors["party"] = "Party size must be a whole number from 1 to 50."
        if mode == EXCEPTION and len(req.reason.strip()) < 5:
            errors["reason"] = "Give a reason of at least 5 characters."
        self._when_errors(errors, setup, now, req.date, req.start, mode)
        return errors

    # ------------------------------------------------------------ table choice (3.2, 3.3)
    def _choose(self, setup: Setup, party: int, start: datetime, end: datetime, chosen_ids: list[int],
                exception: bool, exclude: int | None = None) -> list[int]:
        buffer = setup.values["buffer_minutes"]
        if chosen_ids:
            chosen = [setup.table(i) for i in chosen_ids]
            if any(t is None for t in chosen) or not set_fits(chosen, party, exception):
                raise Refusal("capacity", f"The chosen tables cannot seat a party of {party}.")
            found = self.allocs.conflicts(chosen_ids, start, end, buffer, exclude)
            if found:
                raise Refusal("conflict", "Refused: " + describe_conflict(found[0]) + ".", found[0])
            return list(chosen_ids)
        physical = TableSelector.select(party, setup.tables, exception)
        if physical is None:
            raise Refusal("capacity", f"No table or allowed combination can seat a party of {party}.")
        busy = self.allocs.busy_table_ids(start, end, buffer, exclude)
        if exception and TableSelector.select(party, setup.tables) is None:
            # Above every table maximum: use only the largest permitted sets, never a smaller table.
            sets = TableSelector.exception_sets(party, setup.tables)
            for option in sets:
                if not any(t.id in busy for t in option):
                    return [t.id for t in option]
            blocker = next(busy[t.id] for t in sets[0] if t.id in busy)
            raise Refusal("conflict", "Refused: " + describe_conflict(blocker) + ".", blocker)
        free = [t for t in setup.tables if t.id not in busy]
        pick = TableSelector.select(party, free, exception)
        if pick is None:
            found = self.allocs.conflicts([t.id for t in physical], start, end, buffer, exclude)
            first = found[0] if found else next(iter(busy.values()))
            raise Refusal("conflict", "Refused: " + describe_conflict(first) + ".", first)
        return [t.id for t in pick]

    def _refusal_decision(self, refusal: Refusal, setup: Setup, now: datetime, req_day: str, start: str,
                          party: int, mode: str, exclude: int | None = None) -> Decision:
        alternatives: list[str] = []
        if refusal.kind == "conflict":
            alternatives = self.availability.alternatives_near(
                setup, req_day, start, party, now, online=(mode == ONLINE), exclude_booking_id=exclude)
        return Decision("refused", reason=refusal.kind, message=refusal.message, conflict=refusal.conflict,
                        alternatives=alternatives)

    def _log_refusal(self, refusal: Refusal, actor: Actor, now: datetime) -> None:
        if refusal.kind != "conflict" or refusal.conflict is None:
            return
        c = refusal.conflict
        with transaction(self.conn):
            self.audit.record(now, actor.name, Audit.REFUSED, None,
                              f"overlap on table {c.label} {c.start.strftime('%Y-%m-%d %H:%M')}"
                              f"-{c.end.strftime('%H:%M')}")

    # ------------------------------------------------------------ create
    def create(self, req: BookingRequest, actor: Actor, mode: str) -> Decision:
        refusal: Refusal | None = None
        decision: Decision | None = None
        setup = self.setup_repo.load()
        now = self.rt.clock.now(setup.tz)
        new_ids: list[int] = []
        try:
            with transaction(self.conn):
                setup = self.setup_repo.load()
                now = self.rt.clock.now(setup.tz)
                if mode == ONLINE and not setup.complete:
                    return Decision("refused", reason="unavailable",
                                    message="Online booking is not available yet. Please try again later.")
                if mode == ONLINE:
                    try:
                        if int(str(req.party)) > setup.values["online_max_party"]:
                            return Decision("refused", reason="large_party", message=(
                                f"For parties of more than {setup.values['online_max_party']}, please phone "
                                f"the restaurant on {setup.info.get('contact_phone', '')}."))
                    except ValueError:
                        pass
                errors = self.validate(req, setup, now, mode)
                if errors:
                    return Decision("rejected", errors=errors)
                party = int(str(req.party).strip())
                email = req.email.strip().lower()
                phone = normalise_phone(req.phone)
                duplicate = self._find_duplicate(req, party, email, phone, now)
                if duplicate:
                    return Decision("confirmed", booking=duplicate, duplicate=True,
                                    tables=self.allocs.tables_label_for_booking(duplicate.id))
                start = to_dt(req.date, req.start)
                end = start + minutes(setup.duration_for(party))
                try:
                    table_ids = self._choose(setup, party, start, end, req.table_ids, mode == EXCEPTION)
                except Refusal as r:
                    refusal = r
                    raise
                booking = self.bookings.insert(
                    reference=self.bookings.new_reference(), booking_date=req.date, start_time=start.strftime(T_FMT),
                    party_size=party, channel=Channel.WEBSITE if mode == ONLINE else req.channel,
                    name=req.name.strip(), email=email, phone=phone, step_free=req.step_free,
                    dietary_note=req.dietary.strip(), now=now)
                self.allocs.reserve(table_ids, start, end, setup.values["buffer_minutes"], booking_id=booking.id)
                labels = " + ".join(setup.table(i).label for i in table_ids)
                self.audit.record(now, actor.name, Audit.CREATED, booking.reference,
                                  f"channel {booking.channel}; tables {labels}")
                if mode == EXCEPTION:
                    self.audit.record(now, actor.name, Audit.EXCEPTION, booking.reference, req.reason.strip())
                if booking.guest_email:
                    for purpose in (LinkPurpose.CANCEL, LinkPurpose.CHANGE, LinkPurpose.ATTEND):
                        self.bookings.save_link(booking.id, purpose, make_token(self.rt.secret, booking.reference, purpose))
                new_ids = self.notifier.on_confirmed(booking, setup, now)
                decision = Decision("confirmed", booking=booking, tables=labels)
        except Refusal:
            pass
        except ConflictError as exc:  # lost the race at the very last step; nothing was stored
            refusal = Refusal("conflict", "Refused: " + describe_conflict(exc.conflict) + ".", exc.conflict)
        if refusal is not None:
            self._log_refusal(refusal, actor, now)
            try:
                party = int(str(req.party).strip())
            except ValueError:
                party = 1
            return self._refusal_decision(refusal, self.setup_repo.load(), now, req.date, req.start, party, mode)
        if decision is not None and decision.ok:
            self.notifier.dispatch(new_ids)
        return decision  # type: ignore[return-value]

    def _find_duplicate(self, req: BookingRequest, party: int, email: str, phone: str, now: datetime) -> Booking | None:
        since = fmt_dt(now - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
        row = self.conn.execute(
            """SELECT * FROM bookings WHERE created_at >= ? AND booking_date=? AND start_time=? AND party_size=?
               AND guest_name=? AND COALESCE(guest_email,'')=? AND COALESCE(guest_phone,'')=? AND status='Confirmed'""",
            (since, req.date, req.start, party, req.name.strip(), email, phone)).fetchone()
        return Booking.from_row(row) if row else None

    # ------------------------------------------------------------ change (FR-005, FR-041)
    def change(self, booking_id: int, req: BookingRequest, actor: Actor, mode: str) -> Decision:
        refusal: Refusal | None = None
        decision: Decision | None = None
        setup = self.setup_repo.load()
        now = self.rt.clock.now(setup.tz)
        try:
            with transaction(self.conn):
                setup = self.setup_repo.load()
                now = self.rt.clock.now(setup.tz)
                b = self.bookings.get(booking_id)
                if b is None or b.status != Status.CONFIRMED:
                    return Decision("refused", reason="state", message="Only a Confirmed booking can be changed.")
                full = BookingRequest(
                    date=req.date, start=req.start, party=req.party, name=b.guest_name or "x",
                    email=b.guest_email or "", phone=b.guest_phone or "", privacy_ack=True,
                    channel=b.channel if b.channel != Channel.WEBSITE else Channel.PHONE,
                    table_ids=req.table_ids, reason=req.reason)
                errors = self.validate(full, setup, now, mode)
                errors.pop("name", None)
                if mode == ONLINE:
                    errors.pop("email", None)
                    try:
                        if int(str(req.party)) > setup.values["online_max_party"]:
                            errors["party"] = f"Online changes allow up to {setup.values['online_max_party']} guests."
                    except ValueError:
                        pass
                if errors:
                    return Decision("rejected", errors=errors, booking=b)
                party = int(str(req.party).strip())
                start = to_dt(req.date, req.start)
                end = start + minutes(setup.duration_for(party))
                try:
                    table_ids = self._choose(setup, party, start, end, req.table_ids, mode == EXCEPTION,
                                             exclude=b.id)
                except Refusal as r:
                    refusal = r
                    raise
                old = f"{b.booking_date} {b.start_time} x{b.party_size}"
                self.allocs.release_for_booking(b.id)  # old allocations released in the same step
                self.allocs.reserve(table_ids, start, end, setup.values["buffer_minutes"], booking_id=b.id)
                self.bookings.update_details(b, booking_date=req.date, start_time=start.strftime(T_FMT),
                                             party_size=party, now=now)
                updated = self.bookings.get(b.id)
                labels = " + ".join(setup.table(i).label for i in table_ids)
                self.audit.record(now, actor.name, Audit.CHANGED, b.reference,
                                  f"{old} -> {updated.booking_date} {updated.start_time} x{party}; tables {labels}")
                if mode == EXCEPTION:
                    self.audit.record(now, actor.name, Audit.EXCEPTION, b.reference, req.reason.strip())
                self.notifier.reschedule_reminder(updated, setup, now)
                decision = Decision("confirmed", booking=updated, tables=labels)
        except Refusal:
            pass
        except ConflictError as exc:
            refusal = Refusal("conflict", "Refused: " + describe_conflict(exc.conflict) + ".", exc.conflict)
        if refusal is not None:
            self._log_refusal(refusal, actor, now)
            try:
                party = int(str(req.party).strip())
            except ValueError:
                party = 1
            return self._refusal_decision(refusal, self.setup_repo.load(), now, req.date, req.start, party,
                                          mode, exclude=booking_id)
        return decision  # type: ignore[return-value]

    # ------------------------------------------------------------ walk-in (FR-004)
    def walk_in(self, party_raw, table_ids: list[int], actor: Actor) -> Decision:
        refusal: Refusal | None = None
        setup = self.setup_repo.load()
        now = self.rt.clock.now(setup.tz)
        try:
            party = int(str(party_raw).strip())
            if not 1 <= party <= 50:
                raise ValueError
        except ValueError:
            return Decision("rejected", errors={"party": "Party size must be a whole number from 1 to 50."})
        decision: Decision | None = None
        try:
            with transaction(self.conn):
                setup = self.setup_repo.load()
                now = self.rt.clock.now(setup.tz)
                end = now + minutes(setup.duration_for(party))
                try:
                    ids = self._choose(setup, party, now, end, table_ids, False)
                except Refusal as r:
                    refusal = r
                    raise
                visit_id = self.bookings.create_visit(None, now, party, ids)
                self.allocs.reserve(ids, now, end, setup.values["buffer_minutes"], visit_id=visit_id)
                labels = " + ".join(setup.table(i).label for i in ids)
                self.audit.record(now, actor.name, Audit.CREATED, None, f"walk-in of {party}; tables {labels}")
                decision = Decision("confirmed", tables=labels)
        except Refusal:
            pass
        except ConflictError as exc:
            refusal = Refusal("conflict", "Refused: " + describe_conflict(exc.conflict) + ".", exc.conflict)
        if refusal is not None:
            self._log_refusal(refusal, actor, now)
            return Decision("refused", reason=refusal.kind, message=refusal.message, conflict=refusal.conflict)
        return decision  # type: ignore[return-value]


def _valid_date(text: str) -> bool:
    try:
        datetime.strptime(text, D_FMT)
        return True
    except ValueError:
        return False


def _valid_time(text: str) -> bool:
    try:
        datetime.strptime(text, T_FMT)
        return True
    except ValueError:
        return False
