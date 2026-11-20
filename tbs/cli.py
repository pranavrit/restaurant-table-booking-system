"""Command line tools: flask --app tbs <command>."""
from __future__ import annotations

import secrets

import click
from flask import Flask

from .db import transaction
from .domain import Role


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """Create the tables (safe to run again)."""
        app.services()  # type: ignore[attr-defined]
        click.echo(f"Database ready at {app.config['DATABASE']}")

    @app.cli.command("create-account")
    @click.option("--name", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--role", type=click.Choice([Role.HOST, Role.MANAGER]), default=Role.MANAGER, show_default=True)
    @click.password_option()
    def create_account(name, email, role, password):
        """Create a staff account (the first manager has to be made here)."""
        result = app.services().accounts.create(name, email, role, password)  # type: ignore[attr-defined]
        click.echo(result.message)
        for message in result.errors.values():
            click.echo(f"  - {message}")
        if not result.ok:
            raise SystemExit(1)

    @app.cli.command("run-jobs")
    def run_jobs():
        """Send due reminders and retries, and remove expired personal data. Safe to run from cron."""
        svc = app.services()  # type: ignore[attr-defined]
        sent = svc.notifications.process_due()
        removed = svc.retention.remove_expired()
        click.echo(f"{sent} message(s) sent, personal data removed from {removed} booking(s).")

    @app.cli.command("seed-demo")
    @click.option("--password", default=None, help="Password for the demo accounts (random if left out).")
    def seed_demo(password):
        """Fill an empty database with sample tables, opening hours and two staff accounts."""
        svc = app.services()  # type: ignore[attr-defined]
        if svc.load_setup().tables:
            raise click.ClickException("The database already has tables. Demo data was not added.")
        password = password or secrets.token_urlsafe(12)
        svc.setup.save_info({"restaurant_name": "Demo Bistro", "contact_phone": "+97140000000",
                             "time_zone": "Asia/Dubai"})
        layout = [("T1", 1, 2), ("T2", 1, 2), ("T3", 2, 4), ("T4", 2, 4), ("T5", 4, 6), ("T6", 6, 8)]
        for label, low, high in layout:
            svc.setup.save_table({"label": label, "min_seats": str(low), "max_seats": str(high)})
        by_label = {t.label: t.id for t in svc.load_setup().tables}
        for a, b in (("T1", "T2"), ("T3", "T4")):
            svc.setup.save_table({"label": a, "min_seats": "1" if a == "T1" else "2",
                                  "max_seats": "2" if a == "T1" else "4", "combinable": [str(by_label[b])]},
                                 by_label[a])
        svc.setup.save_period({"name": "Lunch", "days": list("0123456"), "start": "12:00", "end": "15:00",
                               "last_start": "14:00"})
        svc.setup.save_period({"name": "Dinner", "days": list("0123456"), "start": "18:00", "end": "23:00",
                               "last_start": "21:30"})
        svc.accounts.create("Demo Manager", "manager@example.com", Role.MANAGER, password)
        svc.accounts.create("Demo Host", "host@example.com", Role.HOST, password)
        click.echo("Demo data added.")
        click.echo(f"Sign in at /staff/login as manager@example.com or host@example.com with password: {password}")
