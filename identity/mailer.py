"""Minimal SMTP sender for transactional email (password reset).

Provider-neutral: any SMTP server works (Gmail with an app password, company mail, SES,
SendGrid, Mailgun...). Configured only through environment variables; nothing is sent
unless SMTP_HOST and SMTP_FROM are set.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


class Mailer:
    def __init__(self, host, port, user, password, sender, name, security):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.sender, self.name, self.security = sender, name, security

    @classmethod
    def from_env(cls):
        host = os.getenv("SMTP_HOST", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip()
        if not host or not sender:
            return None
        port = int(os.getenv("SMTP_PORT", "587") or "587")
        security = os.getenv("SMTP_SECURITY", "").strip().lower() or (
            "ssl" if port == 465 else "starttls"
        )
        if security not in ("starttls", "ssl", "none"):
            raise ValueError("SMTP_SECURITY must be starttls, ssl or none")
        return cls(
            host,
            port,
            os.getenv("SMTP_USER", "").strip(),
            os.getenv("SMTP_PASSWORD", ""),
            sender,
            os.getenv("SMTP_FROM_NAME", "").strip(),
            security,
        )

    def send(self, to, subject, text):
        msg = EmailMessage()
        msg["From"] = formataddr((self.name, self.sender)) if self.name else self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=15)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)

    def send_quietly(self, to, subject, text):
        """Run in a background thread: a mail failure must never break or slow a request."""
        try:
            self.send(to, subject, text)
            logging.info("password reset email sent")
        except Exception:
            logging.exception("password reset email could not be sent")
