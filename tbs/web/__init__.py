"""Web layer helpers (boundary classes of M3 CD-1B)."""
from __future__ import annotations

from functools import wraps

from flask import current_app, g, redirect, render_template, request, session, url_for

from ..domain import role_may
from ..services.common import Actor


def svc():
    return current_app.services()  # type: ignore[attr-defined]


def current_actor() -> Actor:
    return Actor(g.account["name"], g.account["role"])


def staff_required(action: str):
    """Staff pages show nothing before sign-in (FR-057) and refuse actions outside the role (FR-059)."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            account_id = session.get("account_id")
            row = svc().accounts.accounts.get(account_id) if account_id else None
            if row is None or not row["active"]:
                session.pop("account_id", None)
                return redirect(url_for("staff.login", next=request.full_path if request.method == "GET" else None))
            g.account = row
            if not role_may(row["role"], action):
                return render_template("error.html", error=_Forbidden()), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


class _Forbidden:
    code = 403
    name = "Not permitted"
    description = "Your role does not allow this action."
