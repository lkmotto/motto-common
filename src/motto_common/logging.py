"""Shared logging setup for the Motto fleet.

Every Motto service can call ``setup_logging("service-name")`` to get a
consistent, structured logger with JSON formatting support.
"""

from __future__ import annotations

import logging
import os
import sys


class _JsonFormatter(logging.Formatter):
    """Minimal JSON-line formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = str(record.exc_info[1])
        return json.dumps(payload, default=str)


def setup_logging(
    name: str,
    *,
    level: int | None = None,
    json_fmt: bool = False,
) -> logging.Logger:
    """Create or retrieve a configured logger.

    Args:
        name: The logger name (typically the service / module name).
        level: Logging level override.  When ``None``, the level is read
            from ``LOG_LEVEL`` env var, defaulting to ``logging.INFO``.
        json_fmt: When ``True``, emit JSON-line structured logs.  Otherwise
            use the standard ``"%(asctime)s [%(levelname)s] %(name)s: %(message)s"``
            format.

    Returns:
        A :class:`logging.Logger` with at least one ``StreamHandler`` attached.
    """
    logger = logging.getLogger(name)

    # Only add handlers if they haven't been added yet (idempotent).
    if not logger.handlers:
        if level is None:
            raw = os.getenv("LOG_LEVEL", "INFO").upper()
            level = getattr(logging, raw, logging.INFO)

        logger.setLevel(level)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)

        if json_fmt:
            handler.setFormatter(_JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )

        logger.addHandler(handler)

    return logger
