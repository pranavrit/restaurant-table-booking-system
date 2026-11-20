"""Repositories: one per data store of the Level 0 DFD (D1 to D7)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import (
    DEFAULTS,
    DT_FMT,
    D_FMT,
    ServicePeriod,
    Setup,
    Status,
    Table,
    fmt_dt,
    parse_dt,
    to_dt,
)

REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


@dataclass
class Booking:
    id: int
    reference: str
    booking_date: str
    start_time: str
    party_size: int
    status: str
    late_cancel: bool
    channel: str
    attendance_confirmed_at: str | None
    arrived_at: str | None
    step_free: bool
    dietary_note: str | None
    guest_name: str | None
    guest_email: str | None
    guest_phone: str | None
    personal_removed: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Booking":
        return cls(
            id=r["id"], reference=r["reference"], booking_date=r["booking_date"], start_time=r["start_time"],
            party_size=r["party_size"], status=r["status"], late_cancel=bool(r["late_cancel"]),
            channel=r["channel"], attendance_confirmed_at=r["attendance_confirmed_at"],
            arrived_at=r["arrived_at"], step_free=bool(r["step_free"]), dietary_note=r["dietary_note"],
            guest_name=r["guest_name"], guest_email=r["guest_email"], guest_phone=r["guest_phone"],
            personal_removed=bool(r["personal_removed"]), created_at=r["created_at"], updated_at=r["updated_at"],
        )

    @property
    def start_dt(self) -> datetime:
        return to_dt(self.booking_date, self.start_time)

    @property
    def is_active(self) -> bool:
        return self.status in Status.ACTIVE


@dataclass
class Allocation:
    id: int
    booking_id: int | None
    visit_id: int | None
    table_id: int
    label: str
    start_dt: datetime
    end_dt: datetime
    state: str


# ---------------------------------------------------------------- D3 setup


class SetupRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> Setup:
        c = self.conn
        stored = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}
        values = {k: int(stored.get(k, d)) for k, d in DEFAULTS.items()}
        info = {
            "restaurant_name": stored.get("restaurant_name", "Our Restaurant"),
            "contact_phone": stored.get("contact_phone", ""),
            "time_zone": stored.get("time_zone", ""),
        }
        tables = {
            r["id"]: Table(r["id"], r["label"], r["min_seats"], r["max_seats"])
            for r in c.execute("SELECT * FROM dining_tables")
        }
        for r in c.execute("SELECT table_id, other_id FROM table_combinations"):
            if r["table_id"] in tables:
                tables[r["table_id"]].combinable.add(r["other_id"])
        periods = [
            ServicePeriod(
                r["id"], r["name"], {int(d) for d in r["days"].split(",") if d != ""},
                r["start_time"], r["end_time"], r["last_start"],
            )
            for r in c.execute("SELECT * FROM service_periods ORDER BY start_time, id")
        ]
        closures = {r["day"] for r in c.execute("SELECT day FROM closure_dates")}
        return Setup(info, values, list(tables.values()), periods, closures)

    # settings
    def save_settings(self, items: dict[str, str | int]) -> None:
        for k, v in items.items():
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # tables
    def insert_table(self, label: str, min_seats: int, max_seats: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO dining_tables(label, min_seats, max_seats) VALUES(?,?,?)", (label, min_seats, max_seats)
        )
        return cur.lastrowid

    def update_table(self, table_id: int, label: str, min_seats: int, max_seats: int) -> None:
        self.conn.execute(
            "UPDATE dining_tables SET label=?, min_seats=?, max_seats=? WHERE id=?",
            (label, min_seats, max_seats, table_id),
        )

    def set_combinable(self, table_id: int, others: set[int]) -> None:
        """Symmetric: if A lists B then B lists A (FR-021, AC-FR-021.3)."""
        self.conn.execute("DELETE FROM table_combinations WHERE table_id=? OR other_id=?", (table_id, table_id))
        for o in others:
            if o != table_id:
                self.conn.execute("INSERT OR IGNORE INTO table_combinations VALUES(?,?)", (table_id, o))
                self.conn.execute("INSERT OR IGNORE INTO table_combinations VALUES(?,?)", (o, table_id))

    def delete_table(self, table_id: int) -> None:
        self.conn.execute("DELETE FROM dining_tables WHERE id=?", (table_id,))

    # periods
    def insert_period(self, name, days, start, end, last_start) -> int:
        cur = self.conn.execute(
            "INSERT INTO service_periods(name, days, start_time, end_time, last_start) VALUES(?,?,?,?,?)",
            (name, ",".join(str(d) for d in sorted(days)), start, end, last_start),
        )
        return cur.lastrowid

    def update_period(self, pid, name, days, start, end, last_start) -> None:
        self.conn.execute(
            "UPDATE service_periods SET name=?, days=?, start_time=?, end_time=?, last_start=? WHERE id=?",
            (name, ",".join(str(d) for d in sorted(days)), start, end, last_start, pid),
        )

    def delete_period(self, pid: int) -> None:
        self.conn.execute("DELETE FROM service_periods WHERE id=?", (pid,))

    # closures
    def add_closure(self, day: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO closure_dates(day) VALUES(?)", (day,))

    def remove_closure(self, day: str) -> None:
        self.conn.execute("DELETE FROM closure_dates WHERE day=?", (day,))


# ------------------------------------------------------------- D1 bookings


class BookingRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def new_reference(self) -> str:
        for _ in range(20):
            ref = "TB-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))
            if not self.conn.execute("SELECT 1 FROM bookings WHERE reference=?", (ref,)).fetchone():
                return ref
        raise RuntimeError("could not create a unique booking reference")

    def insert(self, *, reference, booking_date, start_time, party_size, channel, name, email, phone,
               step_free, dietary_note, now: datetime) -> Booking:
        ts = fmt_dt(now)
        cur = self.conn.execute(
            """INSERT INTO bookings(reference, booking_date, start_time, party_size, status, channel,
                   step_free, dietary_note, guest_name, guest_email, guest_phone, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (reference, booking_date, start_time, party_size, Status.CONFIRMED, channel,
             int(step_free), dietary_note or None, name, email or None, phone or None, ts, ts),
        )
        return self.get(cur.lastrowid)

    def get(self, booking_id: int) -> Booking | None:
        r = self.conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return Booking.from_row(r) if r else None

    def by_reference(self, ref: str) -> Booking | None:
        r = self.conn.execute("SELECT * FROM bookings WHERE reference=?", (ref.strip().upper(),)).fetchone()
        return Booking.from_row(r) if r else None

    def for_date(self, day: str) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE booking_date=? ORDER BY start_time, id", (day,)
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def in_range(self, first: str, last: str) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE booking_date BETWEEN ? AND ? ORDER BY booking_date, start_time",
            (first, last),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def active_from(self, day: str) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE booking_date>=? AND status IN ('Confirmed','Arrived','Seated')",
            (day,),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def update_details(self, b: Booking, *, booking_date, start_time, party_size, now: datetime) -> None:
        self.conn.execute(
            "UPDATE bookings SET booking_date=?, start_time=?, party_size=?, updated_at=? WHERE id=?",
            (booking_date, start_time, party_size, fmt_dt(now), b.id),
        )

    def set_status(self, booking_id: int, status: str, now: datetime, *, late: bool | None = None,
                   arrived_at: datetime | None = None) -> None:
        sets, args = ["status=?", "updated_at=?"], [status, fmt_dt(now)]
        if late is not None:
            sets.append("late_cancel=?")
            args.append(int(late))
        if arrived_at is not None:
            sets.append("arrived_at=?")
            args.append(fmt_dt(arrived_at))
        args.append(booking_id)
        self.conn.execute(f"UPDATE bookings SET {', '.join(sets)} WHERE id=?", args)

    def mark_attendance(self, booking_id: int, now: datetime) -> None:
        self.conn.execute(
            "UPDATE bookings SET attendance_confirmed_at=?, updated_at=? WHERE id=?",
            (fmt_dt(now), fmt_dt(now), booking_id),
        )

    def by_email(self, email: str) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE guest_email=? AND personal_removed=0", (email.strip().lower(),)
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def expired_personal(self, cutoff_day: str) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE personal_removed=0 AND booking_date < ?", (cutoff_day,)
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def remove_personal(self, booking_id: int) -> None:
        """Irreversibly remove personal fields; DR-010 fields stay (FR-062)."""
        self.conn.execute(
            """UPDATE bookings SET guest_name=NULL, guest_email=NULL, guest_phone=NULL, dietary_note=NULL,
                   step_free=0, personal_removed=1 WHERE id=?""",
            (booking_id,),
        )
        self.conn.execute("DELETE FROM guest_links WHERE booking_id=?", (booking_id,))

    # guest links (hash only is stored; the token is derived, see links.py)
    def save_link(self, booking_id: int, purpose: str, token: str) -> None:
        h = hashlib.sha256(token.encode()).hexdigest()
        self.conn.execute(
            "INSERT OR IGNORE INTO guest_links(booking_id, purpose, token_hash) VALUES(?,?,?)",
            (booking_id, purpose, h),
        )

    def find_by_token(self, token: str) -> tuple[Booking, str] | None:
        h = hashlib.sha256(token.encode()).hexdigest()
        row = self.conn.execute(
            """SELECT b.*, l.purpose AS link_purpose FROM guest_links l
               JOIN bookings b ON b.id = l.booking_id WHERE l.token_hash=?""",
            (h,),
        ).fetchone()
        if not row:
            return None
        return Booking.from_row(row), row["link_purpose"]

    # visits
    def create_visit(self, booking_id: int | None, arrival: datetime, party: int, table_ids: list[int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO visits(booking_id, arrival, party_size) VALUES(?,?,?)", (booking_id, fmt_dt(arrival), party)
        )
        for t in table_ids:
            self.conn.execute("INSERT INTO visit_tables VALUES(?,?)", (cur.lastrowid, t))
        return cur.lastrowid

    def visit_exists(self, booking_id: int) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM visits WHERE booking_id=?", (booking_id,)).fetchone())

    def walkins_for_date(self, day: str):
        return self.conn.execute(
            """SELECT v.id, v.arrival, v.party_size,
                      (SELECT group_concat(t.label, ' + ') FROM visit_tables vt
                         JOIN dining_tables t ON t.id = vt.table_id WHERE vt.visit_id = v.id) AS tables
               FROM visits v WHERE v.booking_id IS NULL AND substr(v.arrival,1,10)=? ORDER BY v.arrival""",
            (day,),
        ).fetchall()


# ----------------------------------------------------------- D2 allocations


@dataclass
class Conflict:
    table_id: int
    label: str
    start: datetime
    end: datetime


class ConflictError(Exception):
    def __init__(self, conflict: Conflict):
        super().__init__("allocation conflict")
        self.conflict = conflict


class AllocationRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _rows_between(self, lo: datetime, hi: datetime, exclude_booking_id: int | None):
        sql = """SELECT a.*, t.label FROM allocations a JOIN dining_tables t ON t.id = a.table_id
                 WHERE a.state='active' AND a.start_dt < ? AND a.end_dt > ?"""
        args: list = [fmt_dt(hi), fmt_dt(lo)]
        if exclude_booking_id is not None:
            sql += " AND (a.booking_id IS NULL OR a.booking_id <> ?)"
            args.append(exclude_booking_id)
        return self.conn.execute(sql, args).fetchall()

    def conflicts(self, table_ids, start: datetime, end: datetime, buffer: int,
                  exclude_booking_id: int | None = None) -> list[Conflict]:
        """Active allocations of these tables that overlap start..end plus the buffer (BR-01)."""
        ids = set(table_ids)
        pad = timedelta(minutes=buffer)
        out = []
        for r in self._rows_between(start - pad, end + pad, exclude_booking_id):
            if r["table_id"] in ids:
                out.append(Conflict(r["table_id"], r["label"], parse_dt(r["start_dt"]), parse_dt(r["end_dt"])))
        out.sort(key=lambda c: (c.start, c.label))
        return out

    def busy_table_ids(self, start, end, buffer, exclude_booking_id=None) -> dict[int, Conflict]:
        pad = timedelta(minutes=buffer)
        busy: dict[int, Conflict] = {}
        for r in self._rows_between(start - pad, end + pad, exclude_booking_id):
            busy.setdefault(
                r["table_id"], Conflict(r["table_id"], r["label"], parse_dt(r["start_dt"]), parse_dt(r["end_dt"]))
            )
        return busy

    def day_allocations(self, day: str, exclude_booking_id: int | None = None):
        """Active allocations that touch a calendar day, for the availability search."""
        lo = datetime.strptime(day, D_FMT) - timedelta(days=1)
        hi = datetime.strptime(day, D_FMT) + timedelta(days=2)
        return [
            (r["table_id"], parse_dt(r["start_dt"]), parse_dt(r["end_dt"]))
            for r in self._rows_between(lo, hi, exclude_booking_id)
        ]

    def reserve(self, table_ids, start: datetime, end: datetime, buffer: int, *, booking_id=None,
                visit_id=None, exclude_booking_id=None) -> None:
        """Atomic overlap check and write. Raises ConflictError and writes nothing on overlap (FR-018)."""
        found = self.conflicts(table_ids, start, end, buffer, exclude_booking_id)
        if found:
            raise ConflictError(found[0])
        for t in table_ids:
            try:
                self.conn.execute(
                    "INSERT INTO allocations(booking_id, visit_id, table_id, start_dt, end_dt, state) "
                    "VALUES(?,?,?,?,?,'active')",
                    (booking_id, visit_id, t, fmt_dt(start), fmt_dt(end)),
                )
            except sqlite3.IntegrityError as exc:  # second line of defence fired
                label = self.conn.execute("SELECT label FROM dining_tables WHERE id=?", (t,)).fetchone()["label"]
                raise ConflictError(Conflict(t, label, start, end)) from exc

    def release_for_booking(self, booking_id: int) -> None:
        self.conn.execute("UPDATE allocations SET state='released' WHERE booking_id=? AND state='active'", (booking_id,))

    def for_booking(self, booking_id: int, active_only: bool = False) -> list[Allocation]:
        sql = """SELECT a.*, t.label FROM allocations a JOIN dining_tables t ON t.id=a.table_id
                 WHERE a.booking_id=?"""
        if active_only:
            sql += " AND a.state='active'"
        return [self._alloc(r) for r in self.conn.execute(sql + " ORDER BY t.label", (booking_id,))]

    def reactivate_for_booking(self, booking_id: int, buffer: int) -> None:
        """Re-create the allocations of a corrected booking if the tables are still free (FR-048)."""
        mine = self.current_set(booking_id)
        for a in mine:
            found = self.conflicts([a.table_id], a.start_dt, a.end_dt, buffer, exclude_booking_id=booking_id)
            if found:
                raise ConflictError(found[0])
        for a in mine:
            try:
                self.conn.execute("UPDATE allocations SET state='active' WHERE id=?", (a.id,))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(Conflict(a.table_id, a.label, a.start_dt, a.end_dt)) from exc

    @staticmethod
    def _alloc(r) -> Allocation:
        return Allocation(r["id"], r["booking_id"], r["visit_id"], r["table_id"], r["label"],
                          parse_dt(r["start_dt"]), parse_dt(r["end_dt"]), r["state"])

    def future_for_table(self, table_id: int, since: datetime) -> list[Allocation]:
        rows = self.conn.execute(
            """SELECT a.*, t.label FROM allocations a JOIN dining_tables t ON t.id=a.table_id
               WHERE a.table_id=? AND a.state='active' AND a.end_dt > ?""",
            (table_id, fmt_dt(since)),
        )
        return [self._alloc(r) for r in rows]

    def current_set(self, booking_id: int) -> list[Allocation]:
        """The newest group of allocations of a booking (a change creates a new group)."""
        rows = self.for_booking(booking_id)
        if not rows:
            return []
        newest = max(rows, key=lambda a: a.id)
        return [a for a in rows if a.start_dt == newest.start_dt and a.end_dt == newest.end_dt]

    def tables_label_for_booking(self, booking_id: int) -> str:
        return " + ".join(a.label for a in self.current_set(booking_id))


# ------------------------------------------------------ D4 messages, D6 audit


class MessageRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, booking_id: int, mtype: str, scheduled_for: datetime, now: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages(booking_id, type, scheduled_for, state, attempts, last_change) "
            "VALUES(?,?,?,'Queued',0,?)",
            (booking_id, mtype, fmt_dt(scheduled_for), fmt_dt(now)),
        )
        return cur.lastrowid

    def get(self, mid: int):
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def due(self, now: datetime):
        return self.conn.execute(
            "SELECT * FROM messages WHERE state='Queued' AND scheduled_for <= ? ORDER BY id", (fmt_dt(now),)
        ).fetchall()

    def claim(self, mid: int, now: datetime, retry_gap_minutes: int = 5) -> bool:
        """Count the attempt and push the next try forward so two workers never send one message."""
        cur = self.conn.execute(
            """UPDATE messages SET attempts = attempts + 1, scheduled_for = ?, last_change = ?
               WHERE id=? AND state='Queued' AND scheduled_for <= ?""",
            (fmt_dt(now + timedelta(minutes=retry_gap_minutes)), fmt_dt(now), mid, fmt_dt(now)),
        )
        return cur.rowcount == 1

    def set_state(self, mid: int, state: str, now: datetime) -> None:
        self.conn.execute("UPDATE messages SET state=?, last_change=? WHERE id=?", (state, fmt_dt(now), mid))

    def delete_queued_reminders(self, booking_id: int) -> None:
        self.conn.execute(
            "DELETE FROM messages WHERE booking_id=? AND type='reminder' AND state='Queued'", (booking_id,)
        )

    def delete(self, mid: int) -> None:
        self.conn.execute("DELETE FROM messages WHERE id=?", (mid,))

    def sent_since(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE state='Sent' AND last_change >= ?", (fmt_dt(since),)
        ).fetchone()["n"]

    def has_type(self, booking_id: int, mtype: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM messages WHERE booking_id=? AND type=?", (booking_id, mtype)).fetchone())

    def failed_booking_ids(self, booking_ids: list[int]) -> set[int]:
        if not booking_ids:
            return set()
        marks = ",".join("?" * len(booking_ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT booking_id FROM messages WHERE state='Failed' AND booking_id IN ({marks})", booking_ids
        )
        return {r["booking_id"] for r in rows}

    def for_booking(self, booking_id: int):
        return self.conn.execute("SELECT * FROM messages WHERE booking_id=? ORDER BY id", (booking_id,)).fetchall()


class AuditLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, now: datetime, actor: str, action: str, reference: str | None, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(at, actor, action, reference, detail) VALUES(?,?,?,?,?)",
            (fmt_dt(now), actor, action, reference, detail),
        )

    def between(self, first: datetime, last: datetime, action: str | None = None):
        sql = "SELECT * FROM audit_log WHERE at >= ? AND at < ?"
        args = [fmt_dt(first), fmt_dt(last)]
        if action:
            sql += " AND action=?"
            args.append(action)
        return self.conn.execute(sql + " ORDER BY at, id", args).fetchall()

    def purge_older_than(self, cutoff: datetime) -> int:
        return self.conn.execute("DELETE FROM audit_log WHERE at < ?", (fmt_dt(cutoff),)).rowcount


# ----------------------------------------------------------- D7 staff accounts


class AccountRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def by_email(self, email: str):
        return self.conn.execute("SELECT * FROM staff_accounts WHERE email=?", (email.strip(),)).fetchone()

    def get(self, account_id: int):
        return self.conn.execute("SELECT * FROM staff_accounts WHERE id=?", (account_id,)).fetchone()

    def all(self):
        return self.conn.execute("SELECT * FROM staff_accounts ORDER BY name").fetchall()

    def insert(self, name: str, email: str, role: str, password_hash: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO staff_accounts(name, email, role, password_hash) VALUES(?,?,?,?)",
            (name, email.strip(), role, password_hash),
        )
        return cur.lastrowid

    def set_active(self, account_id: int, active: bool) -> None:
        self.conn.execute("UPDATE staff_accounts SET active=? WHERE id=?", (int(active), account_id))

    def active_managers(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM staff_accounts WHERE role='Manager' AND active=1"
        ).fetchone()["n"]

    def record_failure(self, account_id: int, count: int, locked_until: datetime | None) -> None:
        self.conn.execute(
            "UPDATE staff_accounts SET failed_count=?, locked_until=? WHERE id=?",
            (count, fmt_dt(locked_until) if locked_until else None, account_id),
        )

    def reset_failures(self, account_id: int) -> None:
        self.conn.execute("UPDATE staff_accounts SET failed_count=0, locked_until=NULL WHERE id=?", (account_id,))


def make_token(secret: str, reference: str, purpose: str) -> str:
    """Guest link token. Derived from a server secret so it can be rebuilt for a reminder
    while only its hash is stored (NFR-013). Not guessable without the secret."""
    return hmac.new(secret.encode(), f"{reference}|{purpose}".encode(), hashlib.sha256).hexdigest()[:40]
