"""Local Notifier: SMTP if configured, else write to a local outbox so the approval
flow works offline. gcp uses Pub/Sub + an email service; on-prem an SMTP relay."""
from __future__ import annotations

import os
import re
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path


class OutboxNotifier:
    def __init__(self, outbox: str = "data/outbox", **kw):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def notify(self, to: str, subject: str, body: str) -> None:
        host = os.environ.get("SMTP_HOST")
        if host:
            msg = EmailMessage()
            msg["From"] = os.environ.get("SMTP_FROM", "aibox@localhost")
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                if os.environ.get("SMTP_USER"):
                    s.starttls()
                    s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
        else:
            fn = f"{int(time.time() * 1000)}-{re.sub(r'[^a-z0-9]+', '-', to.lower())}.txt"
            (self.outbox / fn).write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n")
