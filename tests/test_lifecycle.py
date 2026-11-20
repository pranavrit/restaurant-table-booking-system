"""Cancellation, attendance, outcomes (processes 4.0 and 6.0)."""
from datetime import datetime

from tbs.domain import Status
from tbs.repositories import make_token

from conftest import DAY


def token(world, ref, purpose):
    return make_token("test-secret", ref, purpose)


def book_at_19(world, **kw):
    return world.online(**kw).booking


def test_cancel_link_before_cutoff_releases_the_table(world):  # AC-FR-038.1
    b = book_at_19(world)
    world.at(f"{DAY} 17:00:00")  # exactly 120 minutes before
    r = world.svc.cancellations.cancel_by_link(token(world, b.reference, "cancel"))
    assert r.ok
    assert world.svc.bookings.bookings.get(b.id).status == Status.CANCELLED
    assert world.svc.conn.execute("SELECT COUNT(*) FROM allocations WHERE state='active'").fetchone()[0] == 0
    assert any("cancelled" in m.subject for m in world.gateway.sent)  # AC-FR-035.1
    assert world.staff(table_ids=[world.table_id("T1")], name="Rebook").ok


def test_cancel_link_one_second_after_cutoff_changes_nothing(world):  # AC-FR-038.2, AC-FR-039.1
    b = book_at_19(world)
    world.at(f"{DAY} 17:00:01")
    r = world.svc.cancellations.cancel_by_link(token(world, b.reference, "cancel"))
    assert not r.ok and r.data["reason"] == "late" and "+97140000000" in r.message
    assert world.svc.bookings.bookings.get(b.id).status == Status.CONFIRMED


def test_bad_links_all_look_the_same_and_leak_nothing(world):  # AC-FR-042.1, AC-FR-042.2
    b = book_at_19(world)
    svc = world.svc.cancellations
    messages = {svc.cancel_by_link(t).message for t in ("", "x" * 200, "deadbeef", token(world, b.reference, "attend"))}
    assert len(messages) == 1
    ok = token(world, b.reference, "cancel")
    assert svc.cancel_by_link(ok).ok
    again = svc.cancel_by_link(ok)
    assert not again.ok and again.message in messages and b.guest_name not in again.message


def test_link_after_the_booking_started_is_invalid(world):
    b = book_at_19(world)
    world.at(f"{DAY} 19:05:00")
    assert not world.svc.cancellations.confirm_attendance(token(world, b.reference, "attend")).ok


def test_attendance_link_marks_the_booking(world):  # AC-FR-034.1
    b = book_at_19(world)
    assert world.svc.cancellations.confirm_attendance(token(world, b.reference, "attend")).ok
    assert world.svc.bookings.bookings.get(b.id).attendance_confirmed_at is not None


def test_staff_late_cancellation_is_marked(world):  # AC-FR-040.1, AC-FR-040.2
    late = world.staff().booking
    early = world.staff(name="Early", start="19:30").booking
    world.at(f"{DAY} 17:30:00")
    assert world.svc.cancellations.cancel_by_staff(late.reference, world.host).data["late"] is True
    world.at(f"{DAY} 17:15:00")
    # 19:30 booking: cutoff is 17:30, so 17:15 is before it
    assert world.svc.cancellations.cancel_by_staff(early.reference, world.host).data["late"] is False
    assert world.svc.bookings.bookings.get(late.id).late_cancel
    assert not world.svc.bookings.bookings.get(early.id).late_cancel


def test_a_seated_booking_cannot_be_cancelled(world):  # AC-DR-001.1
    b = world.staff().booking
    world.at(f"{DAY} 19:00:00")
    assert world.svc.outcomes.mark_seated(b.reference, world.host).ok
    assert not world.svc.cancellations.cancel_by_staff(b.reference, world.host).ok


def test_no_show_only_after_the_grace_period(world):  # AC-FR-046.1, AC-FR-046.2
    b = world.staff().booking
    world.at(f"{DAY} 19:14:00")
    r = world.svc.outcomes.mark_no_show(b.reference, world.host)
    assert not r.ok and "19:15" in r.message
    world.at(f"{DAY} 19:15:00")
    assert world.svc.outcomes.mark_no_show(b.reference, world.host).ok
    assert world.staff(start="19:15", table_ids=[world.table_id("T1")], name="Walk").ok  # table rebookable


def test_arrived_then_seated_records_a_visit(world):  # AC-FR-044.1, AC-FR-045.1
    b = world.staff().booking
    world.at(f"{DAY} 19:02:00")
    assert world.svc.outcomes.mark_arrived(b.reference, world.host).ok
    assert world.svc.outcomes.mark_seated(b.reference, world.host).ok
    visit = world.svc.conn.execute("SELECT * FROM visits WHERE booking_id=?", (b.id,)).fetchone()
    assert visit["arrival"] == f"{DAY} 19:02:00"
    assert not world.svc.outcomes.mark_arrived(b.reference, world.host).ok  # DR-001: no going back


def test_outcome_missing_list(world):  # AC-FR-047.1, AC-FR-047.2
    b = world.staff().booking
    world.at(f"{DAY} 19:15:00")
    assert world.svc.outcomes.outcome_missing() == []
    world.at(f"{DAY} 19:16:00")
    assert [x.reference for x in world.svc.outcomes.outcome_missing()] == [b.reference]
    world.svc.outcomes.mark_seated(b.reference, world.host)
    assert world.svc.outcomes.outcome_missing() == []


def test_manager_correction_logged_and_conflict_checked(world):  # AC-FR-048.1, AC-FR-048.2
    t1 = world.table_id("T1")
    b = world.staff(table_ids=[t1]).booking
    world.at(f"{DAY} 19:20:00")
    world.svc.outcomes.mark_no_show(b.reference, world.host)
    assert world.svc.outcomes.correct(b.reference, "Seated", world.manager).ok
    row = world.svc.conn.execute("SELECT detail FROM audit_log WHERE action='correction'").fetchone()
    assert row["detail"] == "No-show -> Seated"
    # a second booking whose table was re-used blocks a correction
    c = world.staff(start="20:15", table_ids=[t1], name="Next").booking
    world.at(f"{DAY} 20:30:00")
    world.svc.outcomes.mark_no_show(c.reference, world.host)
    d = world.staff(start="20:30", table_ids=[t1], name="Third", date=DAY)
    assert d.ok
    r = world.svc.outcomes.correct(c.reference, "Confirmed", world.manager)
    assert not r.ok and "T1" in r.message
    assert world.svc.bookings.bookings.get(c.id).status == Status.NO_SHOW


def test_correction_window_is_today_or_yesterday(world):
    b = world.staff().booking
    world.at("2026-12-05 10:00:00")
    assert not world.svc.outcomes.correct(b.reference, "Seated", world.manager).ok


def test_day_list_shows_marks_and_walk_ins_in_time_order(world):  # AC-FR-043.1, AC-FR-037.1, AC-FR-012.1
    world.staff(email="", phone="+971501234567", name="Phone Only", start="20:00", step_free=True, dietary="nuts")
    world.staff(name="With Mail", start="19:00")
    world.at(f"{DAY} 19:30:00")
    world.svc.bookings.walk_in("2", [], world.host)
    rows = world.svc.outcomes.day_list(DAY)
    assert [r.start for r in rows] == ["19:00", "19:30", "20:00"]
    phone_only = rows[2]
    assert phone_only.no_email_mark and phone_only.booking.step_free and phone_only.booking.dietary_note == "nuts"
    assert not rows[0].no_email_mark and rows[1].kind == "walkin"
    assert world.svc.outcomes.day_list("2026-12-09") == []
