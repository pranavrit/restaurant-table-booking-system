import sqlite3
import threading

import pytest

from tbs.db import connect
from tbs.domain import Audit, Status
from tbs.services.booking_service import BookingRequest

from conftest import DAY, World


def count(world, sql="SELECT COUNT(*) FROM bookings"):
    return world.svc.conn.execute(sql).fetchone()[0]


def test_online_booking_confirms_and_sends_e_mail(world):  # AC-FR-008.1, AC-FR-031.1
    d = world.online()
    b = d.booking
    assert d.ok and b.status == Status.CONFIRMED and b.channel == "website" and b.reference.startswith("TB-")
    assert d.tables == "T1"
    assert len(world.gateway.sent) == 1
    mail = world.gateway.sent[0]
    assert b.reference in mail.body and "https://tbs.test/cancel/" in mail.body and "grace" in mail.body.lower()
    audit = world.svc.conn.execute("SELECT actor, action FROM audit_log").fetchall()
    assert [(r["actor"], r["action"]) for r in audit] == [("guest link", Audit.CREATED)]


@pytest.mark.parametrize("change,field", [
    ({"email": "guest@"}, "email"),  # AC-FR-009.1
    ({"name": ""}, "name"),
    ({"name": "x" * 101}, "name"),
    ({"privacy_ack": False}, "privacy"),
    ({"start": "19:07"}, "start"),  # AC-FR-009.2: a time the page never lists
    ({"start": "23:00"}, "start"),
    ({"date": "2026-02-30"}, "date"),
])
def test_invalid_online_submissions_create_nothing(world, change, field):
    kw = {"privacy_ack": True}
    kw.update(change)
    d = world.online(**kw)
    assert d.kind == "rejected" and field in d.errors
    assert count(world) == 0 and count(world, "SELECT COUNT(*) FROM allocations") == 0 and world.gateway.sent == []


def test_online_party_above_maximum_is_refused_with_phone(world):
    d = world.online(party="9")
    assert d.kind == "refused" and d.reason == "large_party" and "+97140000000" in d.message


def test_staff_entry_validation_names_each_field(world):  # AC-FR-002.1 to .3, AC-FR-003.1
    d = world.staff(name="", party="0", channel="")
    assert d.kind == "rejected" and {"name", "party", "channel"} <= set(d.errors)
    world.at("2026-12-02 19:00:00")
    d = world.staff(start="18:45")
    assert d.kind == "rejected" and "start" in d.errors


def test_staff_needs_one_contact_detail(world):
    assert "email" in world.staff(email="", phone="").errors
    d = world.staff(email="", phone="+971 50 123 4567")
    assert d.ok and d.booking.guest_phone == "+971501234567" and world.gateway.sent == []  # AC-FR-031.2


def test_email_is_stored_in_lower_case(world):  # AC-DR-003.2
    assert world.staff(email="Guest@Example.ORG").booking.guest_email == "guest@example.org"


def test_second_booking_for_the_same_table_and_time_is_refused(world):  # FR-015, FR-019
    t1 = world.table_id("T1")
    assert world.staff(table_ids=[t1]).ok
    d = world.staff(table_ids=[t1], name="Sam Other", start="19:15")
    assert d.kind == "refused" and d.reason == "conflict"
    assert "T1" in d.message and "19:00" in d.message and "20:15" in d.message
    assert count(world) == 1
    refusal = world.svc.conn.execute("SELECT * FROM audit_log WHERE action=?", (Audit.REFUSED,)).fetchone()
    assert refusal is not None and "Sam" not in refusal["detail"] and refusal["reference"] is None


def test_buffer_and_boundary(make_world):  # AC-FR-015.1 to .3
    w = make_world(tables=[("T1", 1, 2)], durations={"duration_1_2": 90})
    first = w.staff(start="19:00")
    assert first.ok  # ends 20:30
    assert w.staff(start="20:15", name="B").kind == "refused"
    assert w.staff(start="20:30", name="C").ok
    assert w.svc.cancellations.cancel_by_staff(first.booking.reference, w.host).ok
    assert w.staff(start="19:30", name="D").kind == "refused"  # C still holds 20:30; 19:30 overlaps nothing but needs 90 min
    w2 = make_world(tables=[("T1", 1, 2)], buffer=15, durations={"duration_1_2": 90})
    assert w2.staff(start="19:00").ok
    assert w2.staff(start="20:30", name="B").kind == "refused"  # buffer of 15 minutes
    assert w2.staff(start="20:45", name="B").ok


def test_auto_choice_moves_to_next_table_when_first_is_taken(world):  # AC-FR-017.2
    assert world.staff().tables == "T1"
    assert world.staff(name="B").tables == "T2"
    assert world.staff(name="C").tables == "T3"
    d = world.staff(name="D")
    assert d.kind == "refused" and d.reason == "conflict"


def test_conflict_gives_up_to_three_alternatives(make_world):  # AC-FR-011.1
    w = make_world(tables=[("T1", 1, 2)])
    assert w.staff(start="19:00").ok
    d = w.online(start="19:15", name="B")
    assert d.kind == "refused" and d.reason == "conflict"
    assert 1 <= len(d.alternatives) <= 3 and "19:15" not in d.alternatives


def test_party_too_big_for_any_table_is_a_capacity_refusal(world):
    d = world.staff(party="7")
    assert d.kind == "refused" and d.reason == "capacity" and count(world) == 0
    assert "party" in world.staff(party="9").errors  # above the online maximum needs a manager exception


def test_exception_booking_needs_reason_and_is_logged(world):  # AC-FR-020.1, AC-FR-056.1
    no_reason = world.svc.bookings.create(world.req(party="5", reason="no"), world.manager, "exception")
    assert no_reason.kind == "rejected" and "reason" in no_reason.errors
    d = world.svc.bookings.create(world.req(party="5", reason="Regular, birthday"), world.manager, "exception")
    assert d.ok and d.booking.party_size == 5
    row = world.svc.conn.execute("SELECT * FROM audit_log WHERE action=?", (Audit.EXCEPTION,)).fetchone()
    assert row["actor"] == "Mo Manager" and row["detail"] == "Regular, birthday" and row["reference"] == d.booking.reference


def test_exception_still_cannot_overlap(world):  # AC-FR-020.2
    for label in ("T1", "T2", "T3"):
        assert world.staff(table_ids=[world.table_id(label)], party="2", name=label).ok
    d = world.svc.bookings.create(world.req(party="5", reason="Regular, birthday", name="E"), world.manager, "exception")
    assert d.kind == "refused"


def test_same_submission_twice_gives_one_booking(world):  # AC-FR-001.2
    a, b = world.staff(), world.staff()
    assert a.ok and b.ok and b.duplicate and a.booking.reference == b.booking.reference
    assert count(world) == 1


def test_fifty_parallel_requests_for_one_table_confirm_exactly_one(make_world):  # AC-FR-018.1
    w = make_world(tables=[("T1", 1, 2)])
    results, barrier = [], threading.Barrier(20)

    def worker(i):
        s = w.new_services()
        barrier.wait()
        d = s.bookings.create(w.req(name=f"Guest {i}", email=f"g{i}@example.org", privacy_ack=True,
                                    channel="website"), world_actor(), "online")
        results.append(d.kind)

    from tbs.services.common import Actor
    world_actor = lambda: Actor(Actor.GUEST)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("confirmed") == 1 and results.count("refused") == 19
    assert w.svc.conn.execute("SELECT COUNT(*) FROM allocations WHERE state='active'").fetchone()[0] == 1


def test_database_rejects_overlapping_active_allocations_directly(world):  # AC-DR-002.1
    d = world.staff()
    conn = world.svc.conn
    row = conn.execute("SELECT * FROM allocations").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO allocations(booking_id, table_id, start_dt, end_dt, state) VALUES(?,?,?,?, 'active')",
                     (row["booking_id"], row["table_id"], row["start_dt"], row["end_dt"]))


def test_change_moves_booking_and_releases_old_slot(world):  # AC-FR-005.1
    b = world.staff(table_ids=[world.table_id("T1")]).booking
    d = world.svc.bookings.change(b.id, world.req(start="20:00", table_ids=[world.table_id("T1")]), world.host, "staff")
    assert d.ok and d.booking.start_time == "20:00"
    active = world.svc.conn.execute("SELECT start_dt FROM allocations WHERE state='active'").fetchall()
    assert [r[0] for r in active] == [f"{DAY} 20:00:00"]
    assert world.staff(start="18:00", table_ids=[world.table_id("T1")], name="Next").ok


def test_refused_change_leaves_booking_unchanged(world):  # AC-FR-005.2
    t1 = world.table_id("T1")
    b = world.staff(table_ids=[t1]).booking
    assert world.staff(start="20:15", table_ids=[t1], name="Other").ok
    d = world.svc.bookings.change(b.id, world.req(start="20:15", table_ids=[t1]), world.host, "staff")
    assert d.kind == "refused" and "T1" in d.message
    assert world.svc.bookings.bookings.get(b.id).start_time == "19:00"


def test_walk_in_takes_a_table_from_now(world):  # AC-FR-004.1, AC-FR-004.2
    world.at("2026-12-02 19:00:00")
    d = world.svc.bookings.walk_in("2", [], world.host)
    assert d.ok and d.tables == "T1"
    row = world.svc.conn.execute("SELECT * FROM allocations WHERE visit_id IS NOT NULL").fetchone()
    assert row["end_dt"] == f"{DAY} 20:15:00"  # 75-minute default
    # a booking starting in 30 minutes on T2 blocks a walk-in there
    world.at("2026-12-02 18:30:00")
    assert world.staff(start="19:00", table_ids=[world.table_id("T2")], party="3", name="Soon").ok
    blocked = world.svc.bookings.walk_in("3", [world.table_id("T2")], world.host)
    assert blocked.kind == "refused" and "19:00" in blocked.message


def test_booking_data_is_minimal_and_reference_unique(world):  # AC-DR-001.3, FR-061
    refs = {world.staff(name=f"G{i}").booking.reference for i in range(3)}
    assert len(refs) == 3
    cols = {r[1] for r in world.svc.conn.execute("PRAGMA table_info(bookings)")}
    assert {"guest_name", "guest_email", "guest_phone", "dietary_note", "step_free"} <= cols
    assert not {"address", "birthday", "ip_address", "card_number"} & cols
