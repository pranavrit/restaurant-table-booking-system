"""Configuration read from environment variables (names start with TBS_)."""
from __future__ import annotations

import os


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def load_config(instance_path: str) -> dict:
    return {
        "SECRET_KEY": os.environ.get("TBS_SECRET_KEY", "dev-only-secret-change-me"),
        "DATABASE": os.environ.get("TBS_DATABASE", os.path.join(instance_path, "tbs.sqlite3")),
        "BASE_URL": os.environ.get("TBS_BASE_URL", "http://localhost:5000"),
        "MAIL_BACKEND": os.environ.get("TBS_MAIL_BACKEND", "console"),  # console | smtp
        "MAIL_SENDER": os.environ.get("TBS_MAIL_SENDER", "bookings@example.com"),
        "MAIL_OUTBOX": os.environ.get("TBS_MAIL_OUTBOX", os.path.join(instance_path, "outbox")),
        "SMTP_HOST": os.environ.get("TBS_SMTP_HOST", "localhost"),
        "SMTP_PORT": int(os.environ.get("TBS_SMTP_PORT", "587")),
        "SMTP_USER": os.environ.get("TBS_SMTP_USER", ""),
        "SMTP_PASSWORD": os.environ.get("TBS_SMTP_PASSWORD", ""),
        "SMTP_STARTTLS": _flag("TBS_SMTP_STARTTLS", True),
        "MAIL_HOURLY_LIMIT": int(os.environ.get("TBS_MAIL_HOURLY_LIMIT", "0")),  # 0 = no limit (CON-002)
        "FORCE_HTTPS": _flag("TBS_FORCE_HTTPS", False),  # IF-005: redirect HTTP to HTTPS
        "SCHEDULER_ENABLED": _flag("TBS_SCHEDULER", True),
        "ASYNC_EMAIL": _flag("TBS_ASYNC_EMAIL", True),
        "CSRF_ENABLED": True,
        "PERMANENT_SESSION_LIFETIME": 8 * 3600,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
    }
