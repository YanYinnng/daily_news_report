from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

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

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
