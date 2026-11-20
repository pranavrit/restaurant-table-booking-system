"""Guest pages: booking, confirmation, cancellation, change and attendance (IF-001, M3 GuestBookingController)."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from ..domain import D_FMT, LinkPurpose
from ..services.booking_service import ONLINE, BookingRequest
from ..services.common import Actor
from . import svc

bp = Blueprint("guest", __name__)


def _today(setup) -> str:
    return svc().now(setup).strftime(D_FMT)


def _booking_request(form) -> BookingRequest:
    return BookingRequest(
        date=form.get("date", "").strip(), start=form.get("start", "").strip(), party=form.get("party", "").strip(),
        name=form.get("name", ""), email=form.get("email", ""), phone=form.get("phone", ""),
        step_free=form.get("step_free") == "on", dietary=form.get("dietary", ""),
        privacy_ack=form.get("privacy") == "on")


@bp.route("/")
def home():
    return redirect(url_for("guest.book"))


@bp.route("/book")
def book():
    s = svc()
    setup = s.load_setup()
    now = s.now(setup)
    day = request.args.get("date", "").strip()
    party = request.args.get("party", "").strip()
    result = s.availability.search(day, party, setup, now) if (day or party) else None
    return render_template("guest/book.html", setup=setup, day=day or now.strftime(D_FMT), party=party or "2",
                           result=result, searched=result is not None, today=now.strftime(D_FMT))


@bp.route("/api/availability")
def api_availability():
    """Used by the booking page to refresh times without reloading."""
    s = svc()
    setup = s.load_setup()
    r = s.availability.search(request.args.get("date", "").strip(), request.args.get("party", "").strip(),
                              setup, s.now(setup))
    return jsonify(times=r.times, message=r.message, reason=r.reason)


@bp.route("/book/details")
def details():
    setup = svc().load_setup()
    return render_template("guest/details.html", setup=setup, form=request.args, errors={})


@bp.route("/book/confirm", methods=["POST"])
def confirm():
    s = svc()
    decision = s.bookings.create(_booking_request(request.form), Actor(Actor.GUEST), ONLINE)
    setup = s.load_setup()
    if decision.kind == "rejected":
        return render_template("guest/details.html", setup=setup, form=request.form, errors=decision.errors), 422
    if decision.kind == "refused":
        return render_template("guest/refused.html", setup=setup, decision=decision, form=request.form), 409
    return render_template("guest/confirmed.html", setup=setup, decision=decision)


def _link_context(token: str, purpose: str):
    s = svc()
    setup = s.load_setup()
    booking = s.cancellations.resolve_link(token, purpose, s.now(setup))
    return setup, booking


@bp.route("/cancel/<token>", methods=["GET", "POST"])
def cancel(token: str):
    s = svc()
    setup, booking = _link_context(token, LinkPurpose.CANCEL)
    if booking is None:
        return render_template("guest/link_invalid.html", setup=setup), 404
    if request.method == "POST":
        result = s.cancellations.cancel_by_link(token)
        return render_template("guest/cancelled.html", setup=setup, result=result, booking=booking)
    return render_template("guest/cancel.html", setup=setup, booking=booking, token=token)


@bp.route("/attend/<token>", methods=["GET", "POST"])
def attend(token: str):
    s = svc()
    setup, booking = _link_context(token, LinkPurpose.ATTEND)
    if booking is None:
        return render_template("guest/link_invalid.html", setup=setup), 404
    if request.method == "POST":
        result = s.cancellations.confirm_attendance(token)
        return render_template("guest/attended.html", setup=setup, result=result, booking=booking)
    return render_template("guest/attend.html", setup=setup, booking=booking, token=token)


@bp.route("/change/<token>", methods=["GET", "POST"])
def change(token: str):
    s = svc()
    setup, booking = _link_context(token, LinkPurpose.CHANGE)
    if booking is None:
        return render_template("guest/link_invalid.html", setup=setup), 404
    now = s.now(setup)
    cutoff_passed = now > booking.start_dt - timedelta(minutes=setup.values["cutoff_minutes"])
    if cutoff_passed:  # FR-041: changes only at or before the cutoff
        return render_template("guest/change_late.html", setup=setup, booking=booking)
    if request.method == "POST":
        req = BookingRequest(date=request.form.get("date", "").strip(), start=request.form.get("start", "").strip(),
                             party=request.form.get("party", "").strip())
        decision = s.bookings.change(booking.id, req, Actor(Actor.GUEST), ONLINE)
        if decision.ok:
            return render_template("guest/changed.html", setup=setup, decision=decision)
        return render_template("guest/change.html", setup=setup, booking=booking, token=token, decision=decision,
                               form=request.form, times=[], searched=False), 409 if decision.kind == "refused" else 422
    day = request.args.get("date", booking.booking_date)
    party = request.args.get("party", str(booking.party_size))
    times, searched = [], "date" in request.args
    if searched:
        try:
            datetime.strptime(day, D_FMT)
            times = s.availability.start_times(setup, day, int(party), now, exclude_booking_id=booking.id) \
                if 1 <= int(party) <= setup.values["online_max_party"] and day not in setup.closures else []
        except ValueError:
            times = []
    return render_template("guest/change.html", setup=setup, booking=booking, token=token, decision=None,
                           form={"date": day, "party": party}, times=times, searched=searched)
