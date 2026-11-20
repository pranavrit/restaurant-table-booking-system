import re

import pytest

from tbs import create_app
from tbs.repositories import make_token

from conftest import DAY, World

PASSWORD = "correct-horse-battery"


@pytest.fixture
def web(world):
    world.svc.accounts.create("Mo Manager", "mo@example.org", "Manager", PASSWORD)
    world.svc.accounts.create("Hana Host", "hana@example.org", "Host", PASSWORD)
    return world


def sign_in(client, email):
    return client.post("/staff/login", data={"email": email, "password": PASSWORD}, follow_redirects=False)


def test_guest_books_online_end_to_end(web):  # IF-001, FR-007, FR-008, FR-013
    c = web.app.test_client()
    page = c.get(f"/book?date={DAY}&party=2").get_data(as_text=True)
    assert "19:00" in page and "Available times" in page
    form = c.get(f"/book/details?date={DAY}&party=2&start=19:00").get_data(as_text=True)
    for fact in ("15 minutes", "120 minutes", "30 days", "allergen"):  # AC-FR-013.1
        assert fact in form
    r = c.post("/book/confirm", data={"date": DAY, "start": "19:00", "party": "2", "name": "Alex Guest",
                                        "email": "alex@example.org", "privacy": "on"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Your booking is confirmed" in body
    ref = re.search(r"TB-[A-Z0-9]{6}", body).group(0)
    assert ref in web.gateway.sent[0].body


def test_form_errors_appear_next_to_the_field(web):  # AC-IF-001.2
    c = web.app.test_client()
    r = c.post("/book/confirm", data={"date": DAY, "start": "19:00", "party": "2", "name": "A", "email": "guest@",
                                        "privacy": "on"})
    html = r.get_data(as_text=True)
    assert r.status_code == 422 and 'id="email-error"' in html and "valid e-mail" in html


def test_second_guest_sees_alternatives_when_the_table_is_gone(make_world):  # AC-FR-011.1
    w = make_world(tables=[("T1", 1, 2)])
    w.staff(start="19:00")
    c = w.app.test_client()
    r = c.post("/book/confirm", data={"date": DAY, "start": "19:15", "party": "2", "name": "B",
                                        "email": "b@example.org", "privacy": "on"})
    assert r.status_code == 409 and "Nearest free times" in r.get_data(as_text=True)


def test_cancel_and_attend_links_work_through_the_web(web):
    b = web.staff().booking
    c = web.app.test_client()
    cancel_tok = make_token("test-secret", b.reference, "cancel")
    attend_tok = make_token("test-secret", b.reference, "attend")
    assert c.get(f"/attend/{attend_tok}").status_code == 200
    assert "We will see you" in c.post(f"/attend/{attend_tok}").get_data(as_text=True)
    assert b.reference in c.get(f"/cancel/{cancel_tok}").get_data(as_text=True)
    assert "is cancelled" in c.post(f"/cancel/{cancel_tok}").get_data(as_text=True)
    gone = c.get(f"/cancel/{cancel_tok}")
    assert gone.status_code == 404 and b.guest_name not in gone.get_data(as_text=True)
    assert c.get("/cancel/not-a-token").status_code == 404


def test_guest_change_link(web):  # AC-FR-041.1
    b = web.staff().booking
    c = web.app.test_client()
    tok = make_token("test-secret", b.reference, "change")
    assert "20:00" in c.get(f"/change/{tok}?date={DAY}&party=2").get_data(as_text=True)
    r = c.post(f"/change/{tok}", data={"date": DAY, "start": "20:00", "party": "2"})
    assert "Your booking is changed" in r.get_data(as_text=True)
    assert web.svc.bookings.bookings.get(b.id).start_time == "20:00"


def test_staff_pages_need_sign_in(web):  # AC-FR-057.1, AC-IF-002.1
    c = web.app.test_client()
    for url in ("/staff/day", "/staff/booking/new", "/staff/setup", "/staff/reports", "/staff/accounts"):
        r = c.get(url)
        assert r.status_code == 302 and "/staff/login" in r.headers["Location"]
    assert c.post("/staff/booking/TB-AAAAAA/cancel").status_code == 302


def test_sign_in_failure_does_not_say_which_part_was_wrong(web):
    c = web.app.test_client()
    a = c.post("/staff/login", data={"email": "mo@example.org", "password": "nope"})
    b = c.post("/staff/login", data={"email": "who@example.org", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert re.search(r'role="alert">([^<]+)', a.get_data(as_text=True)).group(1) == \
        re.search(r'role="alert">([^<]+)', b.get_data(as_text=True)).group(1)


def test_host_cannot_reach_manager_pages(web):  # AC-FR-059.1
    c = web.app.test_client()
    assert sign_in(c, "hana@example.org").status_code == 302
    for url in ("/staff/setup", "/staff/reports", "/staff/exceptions", "/staff/accounts", "/staff/guest-data"):
        assert c.get(url).status_code == 403
    assert c.post("/staff/setup/settings", data={"grace_minutes": "20"}).status_code == 403
    assert c.get("/staff/day").status_code == 200
    r = c.post("/staff/booking/new", data={"date": DAY, "start": "19:00", "party": "5", "name": "X",
                                             "phone": "+971501234567", "channel": "phone", "exception": "on",
                                             "reason": "party"})
    assert r.status_code == 403


def test_staff_enters_and_manages_a_booking(web):  # FR-001, FR-006, FR-044 to FR-046
    c = web.app.test_client()
    sign_in(c, "hana@example.org")
    r = c.post("/staff/booking/new", data={"date": DAY, "start": "19:00", "party": "2", "name": "Phone Guest",
                                             "phone": "+971 50 123 4567", "channel": "phone"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    ref = re.search(r"TB-[A-Z0-9]{6}", html).group(0)
    assert "Booking confirmed" in html
    day = c.get(f"/staff/day?date={DAY}").get_data(as_text=True)
    assert "Phone Guest" in day and "no e-mail: call if needed" in day
    assert "cancelled" in c.post(f"/staff/booking/{ref}/cancel", follow_redirects=True).get_data(as_text=True).lower()


def test_manager_setup_pages_and_reports(web):
    c = web.app.test_client()
    sign_in(c, "mo@example.org")
    for url in ("/staff/setup", "/staff/reports", "/staff/exceptions", "/staff/accounts", "/staff/guest-data"):
        assert c.get(url).status_code == 200
    bad = c.post("/staff/setup/settings", data={"grace_minutes": "99"})
    assert bad.status_code == 422 and "between 0 and 60" in bad.get_data(as_text=True)
    r = c.post("/staff/setup/table", data={"label": "T9", "min_seats": "2", "max_seats": "4"}, follow_redirects=True)
    assert "Table T9 saved" in r.get_data(as_text=True)
    r = c.get("/staff/reports?first=2026-01-01&last=2027-12-31")
    assert "longer than 366 days" in r.get_data(as_text=True)


def test_guest_text_is_escaped(web):
    web.staff(name="<script>alert(1)</script>")
    c = web.app.test_client()
    sign_in(c, "mo@example.org")
    html = c.get(f"/staff/day?date={DAY}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_login_does_not_redirect_off_site(web):
    c = web.app.test_client()
    r = c.post("/staff/login?next=//evil.example/x", data={"email": "mo@example.org", "password": PASSWORD})
    assert r.status_code == 302 and "evil.example" not in r.headers["Location"]


def test_security_headers_and_no_store(web):
    r = web.app.test_client().get("/book")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


def test_post_without_csrf_token_is_refused(tmp_path):
    w = World(tmp_path).seed()
    app = create_app({"DATABASE": str(tmp_path / "x.sqlite3"), "TESTING": True, "CSRF_ENABLED": True}, clock=w.clock)
    c = app.test_client()
    assert c.post("/staff/login", data={"email": "a@b.co", "password": "x"}).status_code == 400
    page = c.get("/staff/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    assert c.post("/staff/login", data={"email": "a@b.co", "password": "x", "csrf_token": token}).status_code == 401


def test_http_redirects_to_https_when_enabled(tmp_path):  # AC-IF-005.1
    app = create_app({"DATABASE": str(tmp_path / "h.sqlite3"), "TESTING": True, "FORCE_HTTPS": True})
    r = app.test_client().get("/book", base_url="http://tbs.test")
    assert r.status_code == 301 and r.headers["Location"].startswith("https://")
    assert app.test_client().get("/book", base_url="https://tbs.test").status_code == 200


def test_availability_api_matches_the_page(web):
    c = web.app.test_client()
    data = c.get(f"/api/availability?date={DAY}&party=2").get_json()
    assert data["reason"] == "ok" and "19:00" in data["times"]
    assert c.get(f"/api/availability?date={DAY}&party=9").get_json()["reason"] == "large_party"
    assert c.get("/api/availability").get_json()["times"] == []
    assert b"app.js" in c.get("/book").data and c.get("/static/app.js").status_code == 200
