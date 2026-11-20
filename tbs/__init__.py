"""Restaurant Table Booking System (TBS): Flask application factory."""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time

from flask import Flask, abort, g, redirect, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from .clock import Clock
from .config import load_config
from .db import connect, init_db
from .domain import Status
from .services import Runtime, Services
from .services.mail import ConsoleEmailGateway, EmailGateway, SmtpEmailGateway

log = logging.getLogger("tbs")


def create_app(test_config: dict | None = None, gateway: EmailGateway | None = None, clock: Clock | None = None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(load_config(app.instance_path))
    if test_config:
        app.config.update(test_config)
    if app.config["SECRET_KEY"] == "dev-only-secret-change-me" and not app.config.get("TESTING"):
        log.warning("TBS_SECRET_KEY is not set. Set it before real use.")
    if app.config["FORCE_HTTPS"]:
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[method-assign]

    if gateway is None:
        if app.config["MAIL_BACKEND"] == "smtp":
            gateway = SmtpEmailGateway(app.config["SMTP_HOST"], app.config["SMTP_PORT"], app.config["SMTP_USER"],
                                       app.config["SMTP_PASSWORD"], app.config["SMTP_STARTTLS"])
        else:
            gateway = ConsoleEmailGateway(app.config["MAIL_OUTBOX"])
    rt = Runtime(
        secret=app.config["SECRET_KEY"], base_url=app.config["BASE_URL"], sender=app.config["MAIL_SENDER"],
        clock=clock or Clock(), gateway=gateway, db_path=app.config["DATABASE"],
        async_email=app.config["ASYNC_EMAIL"] and not app.config.get("TESTING"),
        hourly_limit=app.config["MAIL_HOURLY_LIMIT"],
    )
    app.extensions["tbs"] = rt

    boot = connect(app.config["DATABASE"])
    init_db(boot)
    boot.close()

    # ---- per-request database connection and services
    def services() -> Services:
        if "svc" not in g:
            g.conn = connect(app.config["DATABASE"])
            g.svc = Services(g.conn, rt)
        return g.svc

    app.services = services  # type: ignore[attr-defined]

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("conn", None)
        g.pop("svc", None)
        if conn is not None:
            conn.close()

    # ---- HTTPS redirect (IF-005), CSRF and security headers
    @app.before_request
    def _guard():
        if app.config["FORCE_HTTPS"] and not request.is_secure:
            return redirect(request.url.replace("http://", "https://", 1), code=301)
        if request.method == "POST" and app.config["CSRF_ENABLED"]:
            sent = request.form.get("csrf_token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "The form expired. Go back, reload the page and try again.")

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; form-action 'self'")
        if request.path.startswith(("/staff", "/cancel", "/change", "/attend", "/book")):
            resp.headers["Cache-Control"] = "no-store"
        if app.config["FORCE_HTTPS"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token  # global, so imported macros can use it

    @app.context_processor
    def _ctx():
        try:
            info = services().load_setup().info
        except Exception:  # never let the layout break an error page
            info = {}
        return {"restaurant": info.get("restaurant_name", "Restaurant"),
                "account": g.get("account"), "Status": Status}

    @app.template_filter("longdate")
    def _longdate(value: str) -> str:
        from datetime import datetime
        return datetime.strptime(value, "%Y-%m-%d").strftime("%A %d %B %Y")

    from .web import guest, staff

    app.register_blueprint(guest.bp)
    app.register_blueprint(staff.bp)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def _error(err):
        return render_template("error.html", error=err), err.code

    from .cli import register_cli
    register_cli(app)
    return app


def start_scheduler(app: Flask, interval_seconds: int = 30) -> threading.Thread | None:
    """Background worker: due reminders, retries and the daily removal of expired personal data.
    Run exactly one process with this enabled."""
    if not app.config["SCHEDULER_ENABLED"]:
        return None
    rt: Runtime = app.extensions["tbs"]

    def loop() -> None:
        conn = connect(rt.db_path)
        svc = Services(conn, rt)
        while True:
            try:
                svc.notifications.process_due()
                if svc.retention.maintenance_due():
                    svc.retention.remove_expired()
            except Exception:
                log.exception("scheduled job failed")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="tbs-scheduler", daemon=True)
    thread.start()
    return thread
