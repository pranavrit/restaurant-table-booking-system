"""Process 8.0, access part: sign-in, lock-out, roles and staff accounts (FR-057 to FR-060)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from ..db import transaction
from ..domain import Role, role_may, valid_email
from ..repositories import AccountRepository
from .common import Result

MAX_FAILURES = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
GENERIC_FAILURE = "The e-mail address or password is not correct."
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class AccountService:
    def __init__(self, conn: sqlite3.Connection, clock, tz_getter):
        self.conn = conn
        self.clock = clock
        self.tz_getter = tz_getter
        self.accounts = AccountRepository(conn)

    def _now(self) -> datetime:
        return self.clock.now(self.tz_getter())

    def sign_in(self, email: str, password: str):
        """Return (account row or None, message). The message never says which of the two was wrong."""
        now = self._now()
        with transaction(self.conn):
            acct = self.accounts.by_email(email or "")
            if acct is None:
                check_password_hash(_DUMMY_HASH, password or "")
                return None, GENERIC_FAILURE
            if not acct["active"]:
                return None, GENERIC_FAILURE
            if acct["locked_until"] and now < datetime.strptime(acct["locked_until"], "%Y-%m-%d %H:%M:%S"):
                return None, "This account is locked for a while after repeated failed sign-ins. Try again later."
            if check_password_hash(acct["password_hash"], password or ""):
                self.accounts.reset_failures(acct["id"])
                return acct, ""
            failures = acct["failed_count"] + 1
            if failures >= MAX_FAILURES:
                self.accounts.record_failure(acct["id"], 0, now + timedelta(minutes=LOCK_MINUTES))
            else:
                self.accounts.record_failure(acct["id"], failures, None)
            return None, GENERIC_FAILURE

    def authorize(self, role: str | None, action: str) -> bool:
        return role_may(role, action)

    def create(self, name: str, email: str, role: str, password: str) -> Result:
        errors: dict[str, str] = {}
        name, email = (name or "").strip(), (email or "").strip().lower()
        if not 1 <= len(name) <= 100:
            errors["name"] = "Name must be 1 to 100 characters."
        if not valid_email(email):
            errors["email"] = "Enter a valid e-mail address."
        if role not in (Role.HOST, Role.MANAGER):
            errors["role"] = "Choose Host or Manager."
        if len(password or "") < MIN_PASSWORD:
            errors["password"] = f"Password must have at least {MIN_PASSWORD} characters."
        if errors:
            return Result.fail("The account was not created.", errors)
        with transaction(self.conn):
            if self.accounts.by_email(email):
                return Result.fail("The account was not created.", {"email": "An account with this e-mail exists."})
            self.accounts.insert(name, email, role, generate_password_hash(password))
        return Result.success(f"Account for {name} created.")

    def set_active(self, account_id: int, active: bool) -> Result:
        with transaction(self.conn):
            acct = self.accounts.get(account_id)
            if acct is None:
                return Result.fail("That account does not exist.")
            if not active and acct["role"] == Role.MANAGER and acct["active"] and self.accounts.active_managers() <= 1:
                return Result.fail("The last active manager account cannot be disabled.")
            self.accounts.set_active(account_id, active)
        return Result.success("Account enabled." if active else "Account disabled.")
