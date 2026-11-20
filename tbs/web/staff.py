"""Staff pages (IF-002): sign-in, day list, booking entry, setup, reports, accounts, guest data."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from ..domain import (
    Channel,
    D_FMT,
    Role,
    SETTING_SPECS,
    Status,
)
from ..services.booking_service import EXCEPTION, STAFF, BookingRequest
from . import current_actor, staff_required, svc

bp = Blueprint("staff", __name__, url_prefix="/staff")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _safe_next(target: str | None) -> str | None:
    return target if target and target.startswith("/staff") and not target.startswith("//") else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    s = svc()
    message = ""
    if request.method == "POST":
        acct, message = s.accounts.sign_in(request.form.get("email", ""), request.form.get("password", ""))
        if acct is not None:
            session.clear()
            session["account_id"] = acct["id"]
            session.permanent = True
            return redirect(_safe_next(request.args.get("next")) or url_for("staff.day"))
    return render_template("staff/login.html", message=message), (401 if message else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("staff.login"))


@bp.route("/")
@staff_required("daylist")
def index():
    return redirect(url_for("staff.day"))


@bp.route("/day")
@staff_required("daylist")
def day():
    s = svc()
    setup = s.load_setup()
    now = s.now(setup)
    chosen = request.args.get("date", now.strftime(D_FMT))
    try:
        from datetime import datetime
        datetime.strptime(chosen, D_FMT)
    except ValueError:
        chosen = now.strftime(D_FMT)
    return render_template("staff/day.html", setup=setup, day=chosen, rows=s.outcomes.day_list(chosen),
                           missing=s.outcomes.outcome_missing(), closed=chosen in setup.closures,
                           grace=setup.values["grace_minutes"], now=now)


# ------------------------------------------------------------------ booking entry and change
def _entry_request(form, mode_exception: bool) -> BookingRequest:
    return BookingRequest(
        date=form.get("date", "").strip(), start=form.get("start", "").strip(), party=form.get("party", "").strip(),
        name=form.get("name", ""), email=form.get("email", ""), phone=form.get("phone", ""),
        step_free=form.get("step_free") == "on", dietary=form.get("dietary", ""),
        channel=form.get("channel", ""), reason=form.get("reason", "") if mode_exception else "",
        table_ids=[int(t) for t in form.getlist("tables") if t.isdigit()])


@bp.route("/booking/new", methods=["GET", "POST"])
@staff_required("booking.create")
def booking_new():
    s = svc()
    setup = s.load_setup()
    form, errors, decision = request.args, {}, None
    if request.method == "POST":
        wants_exception = request.form.get("exception") == "on"
        if wants_exception and g.account["role"] != Role.MANAGER:
            return render_template("error.html", error=type("E", (), {
                "code": 403, "name": "Not permitted", "description": "Only a manager can submit exception bookings."})()), 403
        mode = EXCEPTION if wants_exception else STAFF
        decision = s.bookings.create(_entry_request(request.form, wants_exception), current_actor(), mode)
        form = request.form
        if decision.ok:
            flash(("Booking already recorded" if decision.duplicate else "Booking confirmed")
                  + f": {decision.booking.reference} at table {decision.tables}.", "ok")
            return redirect(url_for("staff.booking_view", reference=decision.booking.reference))
        errors = decision.errors
    return render_template("staff/booking_form.html", setup=setup, form=form, errors=errors, decision=decision,
                           channels=Channel.STAFF, editing=None, default_date=s.now(setup).strftime(D_FMT))


@bp.route("/booking/<reference>")
@staff_required("daylist")
def booking_view(reference: str):
    s = svc()
    setup = s.load_setup()
    b = s.bookings.bookings.by_reference(reference)
    if b is None:
        return render_template("error.html", error=type("E", (), {
            "code": 404, "name": "Not found", "description": "No booking has that reference."})()), 404
    allocs = s.bookings.allocs.for_booking(b.id)
    messages = s.bookings.notifier.messages.for_booking(b.id)
    return render_template("staff/booking.html", setup=setup, b=b, allocs=allocs, messages=messages,
                           tables=s.bookings.allocs.tables_label_for_booking(b.id),
                           now=s.now(setup))


@bp.route("/booking/<reference>/change", methods=["GET", "POST"])
@staff_required("booking.change")
def booking_change(reference: str):
    s = svc()
    setup = s.load_setup()
    b = s.bookings.bookings.by_reference(reference)
    if b is None or b.status != Status.CONFIRMED:
        flash("Only a Confirmed booking can be changed.", "error")
        return redirect(url_for("staff.day"))
    decision, errors = None, {}
    form = {"date": b.booking_date, "start": b.start_time, "party": b.party_size}
    if request.method == "POST":
        wants_exception = request.form.get("exception") == "on"
        if wants_exception and g.account["role"] != Role.MANAGER:
            flash("Only a manager can submit exception bookings.", "error")
            return redirect(url_for("staff.booking_view", reference=reference))
        req = _entry_request(request.form, wants_exception)
        decision = s.bookings.change(b.id, req, current_actor(), EXCEPTION if wants_exception else STAFF)
        form = request.form
        if decision.ok:
            flash(f"Booking changed. Table {decision.tables}.", "ok")
            return redirect(url_for("staff.booking_view", reference=reference))
        errors = decision.errors
    return render_template("staff/booking_form.html", setup=setup, form=form, errors=errors, decision=decision,
                           channels=Channel.STAFF, editing=b, default_date=b.booking_date)


@bp.route("/booking/<reference>/cancel", methods=["POST"])
@staff_required("booking.cancel")
def booking_cancel(reference: str):
    result = svc().cancellations.cancel_by_staff(reference, current_actor())
    flash(result.message, "ok" if result.ok else "error")
    return redirect(url_for("staff.booking_view", reference=reference))


@bp.route("/booking/<reference>/mark/<outcome>", methods=["POST"])
@staff_required("outcome.mark")
def booking_mark(reference: str, outcome: str):
    o = svc().outcomes
    actions = {"arrived": o.mark_arrived, "seated": o.mark_seated, "no-show": o.mark_no_show}
    if outcome not in actions:
        return redirect(url_for("staff.day"))
    result = actions[outcome](reference, current_actor())
    flash(result.message, "ok" if result.ok else "error")
    return redirect(request.form.get("back") if _safe_next(request.form.get("back")) else url_for("staff.day"))


@bp.route("/booking/<reference>/correct", methods=["POST"])
@staff_required("outcome.correct")
def booking_correct(reference: str):
    result = svc().outcomes.correct(reference, request.form.get("status", ""), current_actor())
    flash(result.message, "ok" if result.ok else "error")
    return redirect(url_for("staff.booking_view", reference=reference))


@bp.route("/walkin", methods=["POST"])
@staff_required("walkin")
def walkin():
    tables = [int(t) for t in request.form.getlist("tables") if t.isdigit()]
    decision = svc().bookings.walk_in(request.form.get("party", ""), tables, current_actor())
    if decision.ok:
        flash(f"Walk-in seated at table {decision.tables}.", "ok")
    else:
        flash(decision.message or "; ".join(decision.errors.values()), "error")
    return redirect(url_for("staff.day"))


# ------------------------------------------------------------------ setup (manager)
@bp.route("/setup")
@staff_required("setup")
def setup_page():
    s = svc()
    setup = s.load_setup()
    counts = {d: len([b for b in s.bookings.bookings.for_date(d) if b.status == Status.CONFIRMED])
              for d in sorted(setup.closures)}
    return render_template("staff/setup.html", setup=setup, specs=SETTING_SPECS, days=DAYS, errors={}, form={}, closure_counts=counts)


def _setup_response(result, section: str, extra_errors=None):
    if result.ok:
        flash(result.message, "ok")
        return redirect(url_for("staff.setup_page") + f"#{section}")
    s = svc()
    setup = s.load_setup()
    counts = {d: 0 for d in setup.closures}
    flash(result.message, "error")
    return render_template("staff/setup.html", setup=setup, specs=SETTING_SPECS, days=DAYS, errors=result.errors,
                           form=request.form, closure_counts=counts), 422


@bp.route("/setup/info", methods=["POST"])
@staff_required("setup")
def setup_info():
    return _setup_response(svc().setup.save_info(request.form.to_dict()), "info")


@bp.route("/setup/settings", methods=["POST"])
@staff_required("setup")
def setup_settings():
    return _setup_response(svc().setup.save_settings(request.form.to_dict()), "settings")


@bp.route("/setup/table", methods=["POST"])
@staff_required("setup")
def setup_table():
    raw = {"label": request.form.get("label", ""), "min_seats": request.form.get("min_seats", ""),
           "max_seats": request.form.get("max_seats", ""), "combinable": request.form.getlist("combinable")}
    table_id = request.form.get("table_id")
    return _setup_response(svc().setup.save_table(raw, int(table_id) if table_id and table_id.isdigit() else None),
                           "tables")


@bp.route("/setup/table/<int:table_id>/remove", methods=["POST"])
@staff_required("setup")
def setup_table_remove(table_id: int):
    return _setup_response(svc().setup.remove_table(table_id), "tables")


@bp.route("/setup/period", methods=["POST"])
@staff_required("setup")
def setup_period():
    raw = {"name": request.form.get("name", ""), "days": request.form.getlist("days"),
           "start": request.form.get("start", ""), "end": request.form.get("end", ""),
           "last_start": request.form.get("last_start", "")}
    period_id = request.form.get("period_id")
    return _setup_response(svc().setup.save_period(raw, int(period_id) if period_id and period_id.isdigit() else None),
                           "periods")


@bp.route("/setup/period/<int:period_id>/remove", methods=["POST"])
@staff_required("setup")
def setup_period_remove(period_id: int):
    return _setup_response(svc().setup.remove_period(period_id), "periods")


@bp.route("/setup/closure", methods=["POST"])
@staff_required("setup")
def setup_closure():
    if request.form.get("remove"):
        return _setup_response(svc().setup.remove_closure(request.form["remove"]), "closures")
    return _setup_response(svc().setup.add_closure(request.form.get("day", "")), "closures")


# ------------------------------------------------------------------ reports (manager)
@bp.route("/reports")
@staff_required("reports")
def reports():
    s = svc()
    setup = s.load_setup()
    now = s.now(setup)
    first = request.args.get("first", now.replace(day=1).strftime(D_FMT))
    last = request.args.get("last", now.strftime(D_FMT))
    result = s.reports.summary(first, last)
    return render_template("staff/reports.html", first=first, last=last, result=result)


@bp.route("/exceptions")
@staff_required("exceptions.log")
def exceptions():
    s = svc()
    setup = s.load_setup()
    now = s.now(setup)
    first = request.args.get("first", now.replace(day=1).strftime(D_FMT))
    last = request.args.get("last", now.strftime(D_FMT))
    return render_template("staff/exceptions.html", first=first, last=last, result=s.reports.exception_log(first, last))


# ------------------------------------------------------------------ accounts and guest data (manager)
@bp.route("/accounts", methods=["GET", "POST"])
@staff_required("accounts")
def accounts():
    s = svc()
    errors: dict = {}
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            result = s.accounts.create(request.form.get("name", ""), request.form.get("email", ""),
                                       request.form.get("role", ""), request.form.get("password", ""))
            flash(result.message, "ok" if result.ok else "error")
            if not result.ok:
                errors = result.errors
            else:
                return redirect(url_for("staff.accounts"))
        elif action in ("disable", "enable") and request.form.get("account_id", "").isdigit():
            result = s.accounts.set_active(int(request.form["account_id"]), action == "enable")
            flash(result.message, "ok" if result.ok else "error")
            return redirect(url_for("staff.accounts"))
    return render_template("staff/accounts.html", rows=s.accounts.accounts.all(), errors=errors, form=request.form)


@bp.route("/guest-data", methods=["GET", "POST"])
@staff_required("guestdata.delete")
def guest_data():
    s = svc()
    result = None
    if request.method == "POST":
        result = s.retention.delete_for_email(request.form.get("email", ""), current_actor(),
                                              confirm_cancel=request.form.get("confirm") == "yes")
    return render_template("staff/guest_data.html", result=result, email=request.form.get("email", ""))
