from datetime import timedelta

from tbs.domain import Table
from tbs.services.selection import TableSelector, set_fits

from conftest import DAY


def tables():
    t1, t2, t3 = Table(1, "T1", 1, 2), Table(2, "T2", 2, 4), Table(3, "T3", 2, 4)
    return t1, t2, t3


def test_picks_fewest_total_max_seats_then_lowest_label():  # AC-FR-017.1, AC-FR-017.2
    t1, t2, t3 = tables()
    assert [t.label for t in TableSelector.select(2, [t1, t2, t3])] == ["T1"]
    assert [t.label for t in TableSelector.select(3, [t2, t3])] == ["T2"]
    assert [t.label for t in TableSelector.select(3, [t3])] == ["T3"]


def test_combines_only_tables_marked_combinable():  # AC-FR-016.1, AC-FR-016.2
    a, b = Table(1, "T1", 2, 4, {2}), Table(2, "T2", 2, 4, {1})
    assert sorted(t.label for t in TableSelector.select(7, [a, b])) == ["T1", "T2"]
    a.combinable.clear(); b.combinable.clear()
    assert TableSelector.select(7, [a, b]) is None


def test_big_table_is_not_used_for_a_small_party():  # AC-FR-016.3
    assert TableSelector.select(2, [Table(1, "B1", 6, 8)]) is None


def test_exception_may_exceed_table_maximum():
    t = Table(1, "T1", 2, 4)
    assert TableSelector.select(5, [t]) is None
    assert [x.label for x in TableSelector.select(5, [t], exception=True)] == ["T1"]
    assert not set_fits([t], 5)


def test_lists_only_slots_on_grid_inside_period_and_with_capacity(world):
    s = world.svc
    result = s.availability.search(DAY, 2, s.load_setup(), world.now)
    assert result.times[0] == "18:00" and result.times[-1] == "21:30"
    assert "21:45" not in result.times and "17:45" not in result.times
    assert all(int(t[3:]) % 15 == 0 for t in result.times)


def test_a_booked_only_table_hides_overlapping_start_times(make_world):  # AC-FR-007.1
    w = make_world(tables=[("T1", 1, 2)], durations={"duration_1_2": 90})
    assert w.staff(start="19:00").ok
    times = w.svc.availability.search(DAY, 2, w.svc.load_setup(), w.now).times
    assert not any("19:00" <= t <= "20:15" for t in times)
    assert "20:30" in times


def test_closed_date_lists_nothing(world):  # AC-FR-007.2
    world.svc.setup.add_closure(DAY)
    r = world.svc.availability.search(DAY, 2, world.svc.load_setup(), world.now)
    assert r.times == [] and r.reason == "closed"


def test_minimum_lead_time(world):  # AC-FR-007.3
    world.at("2026-12-02 17:30:00")
    times = world.svc.availability.search(DAY, 2, world.svc.load_setup(), world.now).times
    assert "18:00" not in times and "18:30" in times


def test_party_above_online_maximum_gets_the_phone_number(world):  # AC-FR-010.1
    r = world.svc.availability.search(DAY, 9, world.svc.load_setup(), world.now)
    assert r.times == [] and r.reason == "large_party" and "+97140000000" in r.message
    assert world.svc.availability.search(DAY, 8, world.svc.load_setup(), world.now).reason in ("ok", "full")


def test_online_booking_unavailable_until_set_up(tmp_path):  # AC-FR-014.1
    from conftest import World
    w = World(tmp_path)
    r = w.svc.availability.search(DAY, 2, w.svc.load_setup(), w.now)
    assert r.times == [] and r.reason == "unavailable"
    assert w.online().kind == "refused"


def test_horizon_and_past_dates(world):
    s = world.svc
    setup = s.load_setup()
    assert s.availability.search("2026-11-30", 2, setup, world.now).reason == "past"
    far = (world.now + timedelta(days=61)).strftime("%Y-%m-%d")
    assert s.availability.search(far, 2, setup, world.now).reason == "horizon"
