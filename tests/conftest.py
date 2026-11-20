from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tbs import create_app
from tbs.clock import Clock
from tbs.db import connect
from tbs.domain import Channel, Role
from tbs.services import Services
from tbs.services.booking_service import BookingRequest
from tbs.services.common import Actor
from tbs.services.mail import RecordingEmailGateway

START = datetime(2026, 12, 1, 10, 0, 0)  # a Tuesday
DAY = "2026-12-02"  # Wednesday, inside the horizon


class World:
    """One app with a controllable clock, a recording e-mail gateway and a seeded restaurant."""

    def __init__(self, tmp_path):
        self.now = START
        self.gateway = RecordingEmailGateway()
        self.clock = Clock(fixed=lambda: self.now)
        self.app = create_app({"DATABASE": str(tmp_path / "tbs.sqlite3"), "TESTING": True, "CSRF_ENABLED": False,
                               "SECRET_KEY": "test-secret", "BASE_URL": "https://tbs.test"},
                              gateway=self.gateway, clock=self.clock)
        self.rt = self.app.extensions["tbs"]
        self.svc = self.new_services()
        self.host = Actor("Hana Host", Role.HOST)
        self.manager = Actor("Mo Manager", Role.MANAGER)

    def new_services(self) -> Services:
        return Services(connect(self.app.config["DATABASE"]), self.rt)

    def seed(self, tables=None, buffer=0, durations=None):
        s = self.svc
        assert s.setup.save_info({"restaurant_name": "Test Bistro", "contact_phone": "+97140000000",
                                  "time_zone": "Asia/Dubai"}).ok
        s.setup.save_period({"name": "Dinner", "days": list("0123456"), "start": "18:00", "end": "23:00",
                             "last_start": "21:30"})
        for label, low, high in tables or [("T1", 1, 2), ("T2", 2, 4), ("T3", 2, 4)]:
            assert s.setup.save_table({"label": label, "min_seats": str(low), "max_seats": str(high)}).ok
        settings = {"buffer_minutes": str(buffer)}
        settings.update({k: str(v) for k, v in (durations or {}).items()})
        from tbs.domain import DEFAULTS
        full = {k: str(v) for k, v in DEFAULTS.items()}
        full.update(settings)
        assert s.setup.save_settings(full).ok
        return self

    def table_id(self, label):
        return next(t.id for t in self.svc.load_setup().tables if t.label == label)

    def req(self, **kw) -> BookingRequest:
        base = dict(date=DAY, start="19:00", party="2", name="Alex Guest", email="alex@example.org",
                    phone="", channel=Channel.PHONE)
        base.update(kw)
        return BookingRequest(**base)

    def online(self, **kw):
        kw.setdefault("privacy_ack", True)
        kw.setdefault("channel", Channel.WEBSITE)
        return self.svc.bookings.create(self.req(**kw), Actor(Actor.GUEST), "online")

    def staff(self, **kw):
        return self.svc.bookings.create(self.req(**kw), self.host, "staff")

    def at(self, text: str):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path).seed()


@pytest.fixture
def make_world(tmp_path):
    counter = iter(range(1000))

    def factory(**kw):
        folder = tmp_path / f"w{next(counter)}"
        folder.mkdir()
        return World(folder).seed(**kw)

    return factory
