"""Messages (process 5.0), setup (1.0), reports (7.0), retention and accounts (8.0)."""
from datetime import datetime, timedelta

import pytest

from tbs.domain import Status
from tbs.repositories import make_token
from tbs.services.mail import RecordingEmailGateway

from conftest import DAY


# ------------------------------------------------------------------ reminders and retries
def reminders(world):
    return world.svc.conn.execute("SELECT * FROM messages WHERE type='reminder'").fetchall()


def test_reminder_is_scheduled_at_the_lead_time_and_sent_once(world):  # AC-FR-032.1, AC-FR-032.2
    b = world.staff().booking
    assert [r["scheduled_for"] for r in reminders(world)] == ["2026-12-01 19:00:00"]
    world.at("2026-12-01 19:10:00")
    assert world.svc.notifications.process_due() == 1
    assert world.svc.notifications.process_due() == 0
    mails = [m for m in world.gateway.sent if m.subject.startswith("Reminder")]
    assert len(mails) == 1 and "/attend/" in mails[0].body and "/cancel/" in mails[0].body and b.reference in mails[0].body


def test_no_reminder_for_late_bookings_or_missing_e_mail(world):  # AC-FR-033.2, AC-FR-031.2
    world.at(f"{DAY} 14:00:00")  # 5 hours before a 19:00 start
    world.staff()
    assert reminders(world) == []
    world.at("2026-12-01 10:00:00")
    world.staff(email="", phone="+971501234567", name="Phone", start="20:00")
    assert reminders(world) == []


def test_no_reminder_for_a_cancelled_booking(world):  # AC-FR-033.1
    b = world.staff().booking
    world.svc.cancellations.cancel_by_staff(b.reference, world.host)
    assert reminders(world) == []
    world.at("2026-12-01 20:00:00")
    world.svc.notifications.process_due()
    assert not [m for m in world.gateway.sent if m.subject.startswith("Reminder")]


def test_failed_message_is_retried_three_times_then_marked_failed(world):  # AC-FR-036.1, AC-IF-004
    world.gateway.fail = True
    b = world.staff().booking
    row = lambda: world.svc.conn.execute("SELECT * FROM messages WHERE type='confirmation'").fetchone()
    assert row()["state"] == "Queued" and row()["attempts"] == 1
    for expected in (2, 3):
        world.now += timedelta(minutes=5)
        world.svc.notifications.process_due()
        assert row()["attempts"] == expected and row()["state"] == "Queued"
    world.now += timedelta(minutes=5)
    world.svc.notifications.process_due()
    assert row()["state"] == "Failed" and row()["attempts"] == 4
    world.now += timedelta(minutes=30)
    world.svc.notifications.process_due()
    assert row()["attempts"] == 4
    rows = world.svc.outcomes.day_list(DAY)
    assert rows[0].message_failed


def test_retry_that_succeeds_clears_the_mark(world):  # AC-FR-036.2
    world.gateway.fail = True
    world.staff()
    world.now += timedelta(minutes=5)
    world.svc.notifications.process_due()
    world.gateway.fail = False
    world.now += timedelta(minutes=5)
    world.svc.notifications.process_due()
    assert world.svc.conn.execute("SELECT state FROM messages WHERE type='confirmation'").fetchone()[0] == "Sent"
    assert not world.svc.outcomes.day_list(DAY)[0].message_failed


def test_messages_hold_no_text_or_address(world):  # AC-DR-006.1
    world.staff()
    cols = {r[1] for r in world.svc.conn.execute("PRAGMA table_info(messages)")}
    assert cols == {"id", "booking_id", "type", "scheduled_for", "state", "attempts", "last_change"}


def test_hourly_send_limit_queues_the_rest_in_order(tmp_path):  # AC-CON-002.1
    from conftest import World
    w = World(tmp_path).seed()
    w.rt.hourly_limit = 2
    w.staff(name="A"); w.staff(name="B"); w.staff(name="C")
    assert len(w.gateway.sent) == 2
    w.now += timedelta(hours=1, minutes=1)
    w.svc.notifications.process_due()
    assert len(w.gateway.sent) == 3 and w.gateway.sent[2].body.startswith("Hello C")


def test_guest_links_are_stored_as_hashes_only(world):  # NFR-013
    b = world.staff().booking
    tok = make_token("test-secret", b.reference, "cancel")
    hashes = [r[0] for r in world.svc.conn.execute("SELECT token_hash FROM guest_links")]
    assert tok not in hashes and len(hashes) == 3


# ------------------------------------------------------------------ setup
def test_out_of_range_settings_save_nothing(world):  # AC-FR-024.1, AC-FR-025.1, AC-FR-030.1
    from tbs.domain import DEFAULTS
    values = {k: str(v) for k, v in DEFAULTS.items()}
    values.update(grace_minutes="61", online_max_party="25", retention_days="400", duration_1_2="10", slot_minutes="20")
    r = world.svc.setup.save_settings(values)
    assert not r.ok and {"grace_minutes", "online_max_party", "retention_days", "duration_1_2", "slot_minutes"} <= set(r.errors)
    assert world.svc.load_setup().values["grace_minutes"] == 15
    values.update(grace_minutes="60", online_max_party="20", retention_days="365", duration_1_2="30", slot_minutes="30")
    assert world.svc.setup.save_settings(values).ok
    assert world.svc.load_setup().values["slot_minutes"] == 30


def test_defaults_match_the_srs(tmp_path):  # AC-DR-007.1
    from conftest import World
    v = World(tmp_path).svc.load_setup().values
    assert v == {"grace_minutes": 15, "cutoff_minutes": 120, "buffer_minutes": 0, "slot_minutes": 15,
                 "online_max_party": 8, "horizon_days": 60, "lead_minutes": 60, "reminder_hours": 24,
                 "retention_days": 30, "duration_1_2": 75, "duration_3_4": 90, "duration_5_6": 105,
                 "duration_7_plus": 120}


def test_table_rules(world):  # AC-FR-021.1 to .3
    s = world.svc.setup
    assert "label" in s.save_table({"label": "t1", "min_seats": "1", "max_seats": "2"}).errors
    assert "max_seats" in s.save_table({"label": "X", "min_seats": "5", "max_seats": "4"}).errors
    t1, t2 = world.table_id("T1"), world.table_id("T2")
    assert s.save_table({"label": "T1", "min_seats": "1", "max_seats": "2", "combinable": [str(t2)]}, t1).ok
    assert t1 in world.svc.load_setup().table(t2).combinable  # symmetric
    assert s.save_table({"label": "T1", "min_seats": "1", "max_seats": "2", "combinable": []}, t1).ok
    assert t1 not in world.svc.load_setup().table(t2).combinable  # AC-DR-004.1


def test_service_period_rules(world):  # AC-FR-022.2
    r = world.svc.setup.save_period({"name": "Late", "days": ["0"], "start": "18:00", "end": "23:00", "last_start": "23:30"})
    assert not r.ok and "last_start" in r.errors


def test_cannot_remove_a_table_or_period_in_use(world):  # AC-FR-029.1
    b = world.staff(table_ids=[world.table_id("T3")]).booking
    r = world.svc.setup.remove_table(world.table_id("T3"))
    assert not r.ok and b.reference in r.message
    assert world.svc.load_setup().table(world.table_id("T3")) is not None
    period = world.svc.load_setup().periods[0]
    assert not world.svc.setup.remove_period(period.id).ok
    assert world.svc.setup.remove_table(world.table_id("T2")).ok


def test_closing_a_date_keeps_existing_bookings(world):  # AC-FR-027.1, AC-FR-027.2
    world.staff(); world.staff(name="B")
    r = world.svc.setup.add_closure(DAY)
    assert r.data["existing"] == 2
    assert all(b.status == Status.CONFIRMED for b in world.svc.bookings.bookings.for_date(DAY))
    assert "date" in world.staff(name="C").errors


def test_changed_duration_does_not_move_existing_bookings(world):  # AC-FR-028.1
    world.staff()
    from tbs.domain import DEFAULTS
    v = {k: str(x) for k, x in DEFAULTS.items()}
    v["duration_1_2"] = "120"
    assert world.svc.setup.save_settings(v).ok
    assert world.svc.conn.execute("SELECT end_dt FROM allocations").fetchone()[0] == f"{DAY} 20:15:00"
    world.staff(name="New", start="21:00")
    ends = sorted(r[0] for r in world.svc.conn.execute("SELECT end_dt FROM allocations"))
    assert f"{DAY} 23:00:00" in ends


# ------------------------------------------------------------------ reports
def test_report_counts_rates_and_no_ranking(world):  # AC-FR-051.1, AC-FR-052.1, AC-FR-052.2, AC-FR-054.1
    empty = world.svc.reports.summary("2026-12-01", "2026-12-31").data["report"]
    assert empty.no_show_rate == "no data" and empty.outcome_share == "no data"
    a = world.staff(name="A", start="18:00").booking
    b = world.staff(name="B", start="18:00").booking
    c = world.staff(name="C", start="18:00").booking
    d = world.staff(name="D", start="19:30", table_ids=[world.table_id("T1")]).booking
    world.at(f"{DAY} 18:20:00")
    world.svc.outcomes.mark_seated(a.reference, world.host)
    world.svc.outcomes.mark_no_show(b.reference, world.host)
    world.svc.cancellations.cancel_by_staff(c.reference, world.host)  # 18:20 is after the 16:00 cutoff: late
    world.at(f"{DAY} 22:00:00")
    r = world.svc.reports.summary(DAY, DAY).data["report"]
    assert r.counts["Seated"] == 1 and r.counts["No-show"] == 1 and r.counts["Cancelled late"] == 1
    assert r.counts["Confirmed without outcome"] == 1
    assert r.no_show_rate == "25.0 percent" and "1 no-show out of 4" in r.no_show_detail
    assert r.outcome_share == "75 percent"
    assert not world.svc.reports.summary("2026-01-01", "2027-02-05").ok  # AC-FR-051.2
    assert world.svc.reports.summary("2026-01-01", "2026-12-31").ok


def test_refusals_are_counted(world):
    t1 = world.table_id("T1")
    world.staff(table_ids=[t1]); world.staff(table_ids=[t1], name="B")
    assert world.svc.reports.summary(DAY, "2026-12-31").data["report"].refused_conflicts == 0
    r = world.svc.reports.summary("2026-12-01", "2026-12-31").data["report"]
    assert r.refused_conflicts == 1


def test_exception_log_lists_four_fields(world):  # AC-FR-056.1
    world.svc.bookings.create(world.req(party="5", reason="Regular, birthday"), world.manager, "exception")
    rows = world.svc.reports.exception_log("2026-12-01", "2026-12-31").data["entries"]
    assert len(rows) == 1 and set(rows[0]) == {"date", "actor", "reason", "reference"}


# ------------------------------------------------------------------ retention
def test_personal_data_removed_after_the_retention_period(world):  # AC-FR-062.1, AC-FR-062.2, AC-DR-010.1/2
    old = world.staff(dietary="no nuts", step_free=True).booking
    world.at("2026-12-02 12:00:00")
    young = world.staff(date="2026-12-10", name="Later").booking
    world.at("2027-01-02 10:00:00")  # 31 days after the first booking date
    assert world.svc.retention.remove_expired() == 1
    row = world.svc.conn.execute("SELECT * FROM bookings WHERE id=?", (old.id,)).fetchone()
    assert row["guest_name"] is None and row["guest_email"] is None and row["guest_phone"] is None
    assert row["dietary_note"] is None and row["party_size"] == 2 and row["booking_date"] == DAY
    assert world.svc.bookings.bookings.get(young.id).guest_name == "Later"
    assert world.svc.reports.summary("2026-12-01", "2026-12-31").data["report"].counts["Confirmed without outcome"] == 2
    assert world.svc.conn.execute("SELECT COUNT(*) FROM guest_links WHERE booking_id=?", (old.id,)).fetchone()[0] == 0


def test_booking_dated_29_days_ago_keeps_its_data(world):
    b = world.staff().booking
    world.at("2026-12-31 10:00:00")
    assert world.svc.retention.remove_expired() == 0
    assert world.svc.bookings.bookings.get(b.id).guest_name == "Alex Guest"


def test_audit_entries_older_than_12_months_go(world):  # AC-DR-008.1
    world.svc.conn.execute("INSERT INTO audit_log(at, actor, action, reference, detail) VALUES('2025-11-01 10:00:00','x','created',NULL,'')")
    world.svc.conn.execute("INSERT INTO audit_log(at, actor, action, reference, detail) VALUES('2026-01-15 10:00:00','x','created',NULL,'')")
    world.svc.retention.remove_expired()
    assert [r[0] for r in world.svc.conn.execute("SELECT at FROM audit_log")] == ["2026-01-15 10:00:00"]


def test_delete_on_request_removes_every_booking_of_that_address(world):  # AC-FR-063.1, AC-FR-063.2
    world.at("2026-12-01 10:00:00")
    for day in ("2026-12-02", "2026-12-03"):
        world.staff(date=day, email="guest@example.org", name="G")
    r = world.svc.retention.delete_for_email("Guest@Example.org", world.manager)
    assert not r.ok and r.data["needs_confirmation"]  # AC-FR-064.1: declined means nothing removed
    assert world.svc.bookings.bookings.by_email("guest@example.org")
    r = world.svc.retention.delete_for_email("guest@example.org", world.manager, confirm_cancel=True)  # AC-FR-064.2
    assert r.ok and r.data["count"] == 2
    assert all(b.status == Status.CANCELLED and b.guest_name is None for b in
               world.svc.bookings.bookings.in_range("2026-12-01", "2026-12-31"))
    assert world.svc.conn.execute("SELECT COUNT(*) FROM allocations WHERE state='active'").fetchone()[0] == 0
    assert world.svc.retention.delete_for_email("nobody@example.org", world.manager).data["count"] == 0


# ------------------------------------------------------------------ accounts
def test_account_rules(world):  # AC-DR-009.1, AC-FR-060.1
    a = world.svc.accounts
    assert a.create("Mo", "mo@example.org", "Manager", "long-enough-pw").ok
    assert "email" in a.create("Mo 2", "MO@example.org", "Host", "long-enough-pw").errors
    assert "password" in a.create("Short", "s@example.org", "Host", "short").errors
    mo = a.accounts.by_email("mo@example.org")
    assert not a.set_active(mo["id"], False).ok  # last active manager
    assert a.create("Ann", "ann@example.org", "Manager", "long-enough-pw").ok
    assert a.set_active(mo["id"], False).ok


def test_sign_in_messages_lockout_and_reset(world):  # AC-FR-057.2, AC-FR-058.1, AC-FR-058.2
    a = world.svc.accounts
    a.create("Hana", "hana@example.org", "Host", "correct-horse-battery")
    _, unknown = a.sign_in("nobody@example.org", "whatever")
    _, wrong = a.sign_in("hana@example.org", "wrong")
    assert unknown == wrong
    for _ in range(3):
        a.sign_in("hana@example.org", "wrong")
    assert a.sign_in("hana@example.org", "correct-horse-battery")[0] is not None  # 4 wrong then right: reset
    for _ in range(5):
        a.sign_in("hana@example.org", "wrong")
    assert a.sign_in("hana@example.org", "correct-horse-battery")[0] is None  # locked
    world.now += timedelta(minutes=16)
    assert a.sign_in("hana@example.org", "correct-horse-battery")[0] is not None


def test_permissions_by_role(world):  # AC-FR-059.1, AC-FR-059.2
    a = world.svc.accounts
    for action in ("setup", "reports", "booking.exception", "outcome.correct", "accounts", "guestdata.delete", "exceptions.log"):
        assert not a.authorize("Host", action) and a.authorize("Manager", action)
    for action in ("booking.create", "booking.change", "booking.cancel", "walkin", "daylist", "outcome.mark"):
        assert a.authorize("Host", action) and a.authorize("Manager", action)
    assert not a.authorize(None, "daylist")
