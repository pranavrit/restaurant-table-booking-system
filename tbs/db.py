"""SQLite storage: schema, connections and transactions.

The allocation table carries a trigger that rejects overlapping active allocations of one
table even if the application check is bypassed (DR-002, M3 DD-02).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dining_tables (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(label) BETWEEN 1 AND 20),
    min_seats INTEGER NOT NULL CHECK (min_seats >= 1),
    max_seats INTEGER NOT NULL CHECK (max_seats >= min_seats AND max_seats <= 50)
);

CREATE TABLE IF NOT EXISTS table_combinations (
    table_id INTEGER NOT NULL REFERENCES dining_tables(id) ON DELETE CASCADE,
    other_id INTEGER NOT NULL REFERENCES dining_tables(id) ON DELETE CASCADE,
    PRIMARY KEY (table_id, other_id),
    CHECK (table_id <> other_id)
);

CREATE TABLE IF NOT EXISTS service_periods (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    days TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    last_start TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closure_dates (
    day TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL UNIQUE,
    booking_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    party_size INTEGER NOT NULL CHECK (party_size BETWEEN 1 AND 50),
    status TEXT NOT NULL CHECK (status IN ('Confirmed','Arrived','Seated','Cancelled','No-show')),
    late_cancel INTEGER NOT NULL DEFAULT 0,
    channel TEXT NOT NULL,
    attendance_confirmed_at TEXT,
    arrived_at TEXT,
    step_free INTEGER NOT NULL DEFAULT 0,
    dietary_note TEXT,
    guest_name TEXT,
    guest_email TEXT,
    guest_phone TEXT,
    personal_removed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(booking_date);
CREATE INDEX IF NOT EXISTS idx_bookings_email ON bookings(guest_email);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER REFERENCES bookings(id),
    arrival TEXT NOT NULL,
    party_size INTEGER NOT NULL CHECK (party_size BETWEEN 1 AND 50)
);
CREATE TABLE IF NOT EXISTS visit_tables (
    visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    table_id INTEGER NOT NULL REFERENCES dining_tables(id),
    PRIMARY KEY (visit_id, table_id)
);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER REFERENCES bookings(id),
    visit_id INTEGER REFERENCES visits(id),
    table_id INTEGER NOT NULL REFERENCES dining_tables(id),
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','released')),
    CHECK (end_dt > start_dt),
    CHECK ((booking_id IS NULL) <> (visit_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_alloc_table ON allocations(table_id, state, start_dt);
CREATE INDEX IF NOT EXISTS idx_alloc_booking ON allocations(booking_id);

CREATE TRIGGER IF NOT EXISTS alloc_no_overlap_insert
BEFORE INSERT ON allocations
WHEN NEW.state = 'active' AND EXISTS (
    SELECT 1 FROM allocations a
    WHERE a.table_id = NEW.table_id AND a.state = 'active'
      AND a.start_dt < NEW.end_dt AND a.end_dt > NEW.start_dt)
BEGIN
    SELECT RAISE(ABORT, 'allocation overlap');
END;

CREATE TRIGGER IF NOT EXISTS alloc_no_overlap_update
BEFORE UPDATE ON allocations
WHEN NEW.state = 'active' AND EXISTS (
    SELECT 1 FROM allocations a
    WHERE a.id <> NEW.id AND a.table_id = NEW.table_id AND a.state = 'active'
      AND a.start_dt < NEW.end_dt AND a.end_dt > NEW.start_dt)
BEGIN
    SELECT RAISE(ABORT, 'allocation overlap');
END;

CREATE TABLE IF NOT EXISTS guest_links (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('cancel','change','attend')),
    token_hash TEXT NOT NULL UNIQUE,
    UNIQUE (booking_id, purpose)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    type TEXT NOT NULL CHECK (type IN ('confirmation','reminder','cancellation')),
    scheduled_for TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('Queued','Sent','Failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_change TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_due ON messages(state, scheduled_for);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reference TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

CREATE TABLE IF NOT EXISTS staff_accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('Host','Manager')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock first, so the overlap check and the write
    that follows it are one atomic step (FR-018). Joins an open transaction if there is one."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
