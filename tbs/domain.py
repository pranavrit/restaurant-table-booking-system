"""Domain vocabulary, setting rules and setup value objects (M3 CD-1A)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DT_FMT = "%Y-%m-%d %H:%M:%S"
D_FMT = "%Y-%m-%d"
T_FMT = "%H:%M"


class Status:
    CONFIRMED = "Confirmed"
    ARRIVED = "Arrived"
    SEATED = "Seated"
    CANCELLED = "Cancelled"
    NO_SHOW = "No-show"

    ALL = (CONFIRMED, ARRIVED, SEATED, CANCELLED, NO_SHOW)
    ACTIVE = (CONFIRMED, ARRIVED, SEATED)
    # DR-001: allowed changes outside a manager correction
    ALLOWED = {
        CONFIRMED: {ARRIVED, SEATED, CANCELLED, NO_SHOW},
        ARRIVED: {SEATED},
    }


class Channel:
    WEBSITE = "website"
    PHONE = "phone"
    IN_PERSON = "in_person"
    MESSAGE = "message"
    PLATFORM = "platform"

    STAFF = {
        PHONE: "Phone",
        IN_PERSON: "In person",
        MESSAGE: "Message or social media",
        PLATFORM: "Third-party platform (entered by staff)",
    }


class MsgType:
    CONFIRMATION = "confirmation"
    REMINDER = "reminder"
    CANCELLATION = "cancellation"


class MsgState:
    QUEUED = "Queued"
    SENT = "Sent"
    FAILED = "Failed"


class LinkPurpose:
    CANCEL = "cancel"
    CHANGE = "change"
    ATTEND = "attend"


class Role:
    HOST = "Host"
    MANAGER = "Manager"


class Audit:
    CREATED = "created"
    CHANGED = "changed"
    CANCELLED = "cancelled"
    STATUS = "status change"
    EXCEPTION = "exception"
    CORRECTED = "correction"
    REFUSED = "refusal"
    REMOVAL = "personal data removal"


# FR-059: permitted operations per role. The manager may do everything a host can.
HOST_ACTIONS = frozenset({"booking.create", "booking.change", "booking.cancel", "walkin", "daylist", "outcome.mark"})
MANAGER_ACTIONS = HOST_ACTIONS | frozenset(
    {"booking.exception", "setup", "reports", "exceptions.log", "outcome.correct", "accounts", "guestdata.delete"}
)


def role_may(role: str | None, action: str) -> bool:
    if role == Role.MANAGER:
        return action in MANAGER_ACTIONS
    if role == Role.HOST:
        return action in HOST_ACTIONS
    return False


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    default: int
    minimum: int
    maximum: int
    unit: str = ""
    choices: tuple[int, ...] | None = None


# DR-007 and section 3.6.1 of the M2 SRS
SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("grace_minutes", "Grace period", 15, 0, 60, "minutes"),
    SettingSpec("cutoff_minutes", "Cancellation cutoff (before start)", 120, 0, 1440, "minutes"),
    SettingSpec("buffer_minutes", "Buffer between bookings", 0, 0, 60, "minutes"),
    SettingSpec("slot_minutes", "Slot interval", 15, 15, 30, "minutes", choices=(15, 30)),
    SettingSpec("online_max_party", "Online maximum party size", 8, 1, 20, "guests"),
    SettingSpec("horizon_days", "Booking horizon", 60, 1, 365, "days"),
    SettingSpec("lead_minutes", "Online minimum lead time", 60, 0, 1440, "minutes"),
    SettingSpec("reminder_hours", "Reminder lead time", 24, 1, 72, "hours"),
    SettingSpec("retention_days", "Guest data retention (after booking date)", 30, 1, 365, "days"),
    SettingSpec("duration_1_2", "Dining duration, party of 1 to 2", 75, 30, 240, "minutes"),
    SettingSpec("duration_3_4", "Dining duration, party of 3 to 4", 90, 30, 240, "minutes"),
    SettingSpec("duration_5_6", "Dining duration, party of 5 to 6", 105, 30, 240, "minutes"),
    SettingSpec("duration_7_plus", "Dining duration, party of 7 or more", 120, 30, 240, "minutes"),
)
SPEC_BY_KEY = {s.key: s for s in SETTING_SPECS}
DEFAULTS = {s.key: s.default for s in SETTING_SPECS}

PHONE_RE = re.compile(r"^\+?\d{7,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_phone(raw: str | None) -> str:
    return re.sub(r"[\s\-().]", "", raw or "")


def valid_phone(raw: str | None) -> bool:
    return bool(PHONE_RE.match(normalise_phone(raw)))


def valid_email(raw: str | None) -> bool:
    raw = (raw or "").strip()
    return bool(raw) and len(raw) <= 254 and bool(EMAIL_RE.match(raw))


def validate_settings(raw: dict[str, str]) -> tuple[dict[str, int], dict[str, str]]:
    """Return (values, errors). Nothing is saved by the caller if errors is not empty (FR-030)."""
    values: dict[str, int] = {}
    errors: dict[str, str] = {}
    for spec in SETTING_SPECS:
        if spec.key not in raw:
            continue
        text = str(raw[spec.key]).strip()
        try:
            number = int(text)
        except ValueError:
            errors[spec.key] = f"{spec.label}: enter a whole number."
            continue
        if spec.choices is not None and number not in spec.choices:
            errors[spec.key] = f"{spec.label}: must be one of {', '.join(map(str, spec.choices))}."
        elif not spec.minimum <= number <= spec.maximum:
            errors[spec.key] = f"{spec.label}: must be between {spec.minimum} and {spec.maximum} {spec.unit}."
        else:
            values[spec.key] = number
    return values, errors


def validate_restaurant_info(raw: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    info: dict[str, str] = {}
    errors: dict[str, str] = {}
    if "restaurant_name" in raw:
        name = raw["restaurant_name"].strip()
        if not 1 <= len(name) <= 100:
            errors["restaurant_name"] = "Restaurant name must be 1 to 100 characters."
        else:
            info["restaurant_name"] = name
    if "contact_phone" in raw:
        phone = normalise_phone(raw["contact_phone"])
        if not PHONE_RE.match(phone):
            errors["contact_phone"] = "Contact phone must have 7 to 15 digits, with an optional leading +."
        else:
            info["contact_phone"] = phone
    if "time_zone" in raw:
        tz = raw["time_zone"].strip()
        try:
            ZoneInfo(tz)
            info["time_zone"] = tz
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            errors["time_zone"] = "Unknown time zone. Use a name such as Asia/Dubai."
    return info, errors


def label_key(label: str):
    """Natural sort key so that T2 comes before T10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", label)]


@dataclass
class Table:
    id: int
    label: str
    min_seats: int
    max_seats: int
    combinable: set[int] = field(default_factory=set)

    def fits(self, party: int) -> bool:
        return self.min_seats <= party <= self.max_seats


@dataclass
class ServicePeriod:
    id: int
    name: str
    days: set[int]  # Monday = 0
    start: str
    end: str
    last_start: str

    def runs_on(self, day: date) -> bool:
        return day.weekday() in self.days

    def accepts(self, hhmm: str) -> bool:
        return self.start <= hhmm <= self.last_start


@dataclass
class Setup:
    """One consistent read of the restaurant setup (M3 SetupSnapshot)."""

    info: dict[str, str]
    values: dict[str, int]
    tables: list[Table]
    periods: list[ServicePeriod]
    closures: set[str]

    @property
    def tz(self) -> str:
        return self.info.get("time_zone", "")

    @property
    def complete(self) -> bool:  # FR-014
        return bool(self.tables and self.periods and self.info.get("contact_phone") and self.tz)

    def duration_for(self, party: int) -> int:
        if party <= 2:
            return self.values["duration_1_2"]
        if party <= 4:
            return self.values["duration_3_4"]
        if party <= 6:
            return self.values["duration_5_6"]
        return self.values["duration_7_plus"]

    def table(self, table_id: int) -> Table | None:
        return next((t for t in self.tables if t.id == table_id), None)

    def period_for(self, day: date, hhmm: str) -> ServicePeriod | None:
        for p in self.periods:
            if p.runs_on(day) and p.accepts(hhmm):
                return p
        return None


def to_dt(day: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{day} {hhmm}", f"{D_FMT} {T_FMT}")


def fmt_dt(dt: datetime) -> str:
    return dt.strftime(DT_FMT)


def parse_dt(text: str) -> datetime:
    return datetime.strptime(text, DT_FMT)


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
