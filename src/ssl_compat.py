from __future__ import annotations

import logging


def enable_system_trust_store() -> None:
    try:
        import truststore
    except ImportError:
        return

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        logging.debug("Could not enable system trust store: %s", exc)
