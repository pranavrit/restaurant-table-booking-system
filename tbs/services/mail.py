"""E-mail integration (IF-003, IF-004). NotificationService depends only on EmailGateway."""
from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from ..domain import MsgState

log = logging.getLogger("tbs.mail")
SEND_TIMEOUT_SECONDS = 30  # IF-004: no acceptance within 30 seconds counts as not accepted


@dataclass
class OutboundMessage:
    to: str
    subject: str
    body: str
    sender: str


class EmailGateway(ABC):
    @abstractmethod
    def send(self, message: OutboundMessage) -> str:
        """Hand one message over. Return MsgState.SENT or MsgState.FAILED."""


class SmtpEmailGateway(EmailGateway):
    def __init__(self, host: str, port: int, user: str = "", password: str = "", starttls: bool = True):
        self.host, self.port, self.user, self.password, self.starttls = host, port, user, password, starttls

    def send(self, message: OutboundMessage) -> str:
        mail = EmailMessage()
        mail["From"], mail["To"], mail["Subject"] = message.sender, message.to, message.subject
        mail.set_content(message.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=SEND_TIMEOUT_SECONDS) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(mail)
            return MsgState.SENT
        except Exception as exc:  # any refusal, timeout or network error means "not accepted"
            log.warning("SMTP hand-over failed: %s", exc.__class__.__name__)
            return MsgState.FAILED


class ConsoleEmailGateway(EmailGateway):
    """Development gateway. Writes each message to a text file instead of sending it."""

    def __init__(self, outbox: str | None = None):
        self.outbox = Path(outbox) if outbox else None

    def send(self, message: OutboundMessage) -> str:
        text = f"To: {message.to}\nFrom: {message.sender}\nSubject: {message.subject}\n\n{message.body}\n"
        if self.outbox:
            self.outbox.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".txt"
            (self.outbox / name).write_text(text, encoding="utf-8")
        log.info("e-mail (not sent, development gateway) to %s: %s", message.to, message.subject)
        return MsgState.SENT


class RecordingEmailGateway(EmailGateway):
    """Test double. Can be told to fail."""

    def __init__(self, fail: bool = False):
        self.sent: list[OutboundMessage] = []
        self.fail = fail

    def send(self, message: OutboundMessage) -> str:
        if self.fail:
            return MsgState.FAILED
        self.sent.append(message)
        return MsgState.SENT
