from __future__ import annotations

import os
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from .config import ConfigError


def send_email(subject: str, text_body: str, html_body: str) -> None:
    required = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "MAIL_FROM",
        "MAIL_TO",
    ]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ConfigError("Missing email environment variables: " + ", ".join(missing))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    mail_from = os.environ["MAIL_FROM"]
    recipients = [item.strip() for item in os.environ["MAIL_TO"].split(",") if item.strip()]
    if not recipients:
        raise ConfigError("MAIL_TO must contain at least one recipient")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message["Message-ID"] = make_msgid()
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            refused = smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            refused = smtp.send_message(message)
    if refused:
        raise RuntimeError(f"SMTP refused {len(refused)} of {len(recipients)} configured recipients")
    logging.info("SMTP accepted report email: subject=%s recipients=%d message_id=%s", subject, len(recipients), message["Message-ID"])
