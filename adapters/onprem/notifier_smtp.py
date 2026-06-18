"""
On-prem Notifier: an internal SMTP relay (Postfix / Exchange connector / corporate
smarthost). Pure stdlib smtplib + email.message; no third-party SDK.

Backing service + config:
  SMTP_HOST   relay host (required to actually send; otherwise a clear RuntimeError)
  SMTP_PORT   relay port (default 587)
  SMTP_FROM   envelope/from address (default 'aibox@localhost')
  SMTP_USER   optional auth user; if set, STARTTLS + LOGIN is used
  SMTP_PASS   optional auth password
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


class SmtpNotifier:
    def __init__(self, host: str | None = None, port: int | None = None,
                 sender: str | None = None, user: str | None = None,
                 password: str | None = None, use_tls: bool = True, **kw):
        self.host = host or os.environ.get("SMTP_HOST", "")
        self.port = int(port or os.environ.get("SMTP_PORT", "587"))
        self.sender = sender or os.environ.get("SMTP_FROM", "aibox@localhost")
        self.user = user or os.environ.get("SMTP_USER", "")
        self.password = password or os.environ.get("SMTP_PASS", "")
        self.use_tls = use_tls

    def notify(self, to: str, subject: str, body: str) -> None:
        if not self.host:
            raise RuntimeError("smtp-relay-unset: set SMTP_HOST")
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=15) as s:
            if self.user:
                if self.use_tls:
                    s.starttls(context=ssl.create_default_context())
                s.login(self.user, self.password)
            s.send_message(msg)
