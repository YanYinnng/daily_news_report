from __future__ import annotations

import logging
import os
import re


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for key in ("JUSTONE_API_TOKEN", "LLM_API_KEY", "SMTP_PASSWORD"):
            value = os.getenv(key)
            if value:
                text = text.replace(value, "***REDACTED***")
        return re.sub(r"(?i)([?&](?:token|api_key|access_token)=)[^&\s\"']+", r"\1***REDACTED***", text)
