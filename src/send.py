"""Gmail SMTP delivery (SSL, port 465). Credentials from env only."""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger("newsbot.send")


def send(subject: str, html: str, to_addr: str, from_addr: str) -> None:
    """Send an HTML email via Gmail SMTP over SSL.

    Reads GMAIL_USER and GMAIL_APP_PASSWORD from the environment (a Google
    *App Password*, not the account password). Raises on failure so the caller
    can decide what to do — main.py's top-level handler turns that into a
    visible error email where possible.
    """
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "GMAIL_USER / GMAIL_APP_PASSWORD not set — cannot send email. "
            "Set them as env vars (locally) or GitHub Actions secrets."
        )

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, password)
        s.send_message(msg)
    log.info("sent '%s' -> %s", subject, to_addr)
