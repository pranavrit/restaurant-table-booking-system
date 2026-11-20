"""Small result types shared by the services."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..repositories import Booking, Conflict


@dataclass
class Actor:
    """The person or channel behind an action. Name goes to the audit log (DR-008)."""

    name: str
    role: str | None = None  # None for a guest acting through a link

    GUEST = "guest link"


@dataclass
class Result:
    ok: bool
    message: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    @classmethod
    def success(cls, message: str = "", **data) -> "Result":
        return cls(True, message, {}, data)

    @classmethod
    def fail(cls, message: str = "", errors: dict[str, str] | None = None, **data) -> "Result":
        return cls(False, message, errors or {}, data)


@dataclass
class Decision:
    """What a booking request returns (M3 'Decision'): confirmed, rejected or refused."""

    kind: str  # confirmed | rejected | refused
    booking: Booking | None = None
    errors: dict[str, str] = field(default_factory=dict)
    reason: str = ""  # capacity | conflict | large_party | unavailable | state
    message: str = ""
    conflict: Conflict | None = None
    alternatives: list[str] = field(default_factory=list)
    tables: str = ""
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.kind == "confirmed"
