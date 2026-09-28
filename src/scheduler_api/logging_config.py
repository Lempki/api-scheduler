"""Structured JSON logging for every discord-api-* service.

Each record becomes one JSON object per line, which log collectors can parse without guessing.
The previous format string produced invalid JSON whenever a message contained a quote.
"""

import json
import logging
import logging.config
from typing import Any

__all__ = ["JsonFormatter", "configure_logging"]


class JsonFormatter(logging.Formatter):
    """Renders a log record as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Routes every logger, including uvicorn's, through the JSON formatter.

    Args:
        level: The minimum level, such as "INFO".
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "console": {"class": "logging.StreamHandler", "formatter": "json"}
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                # Uvicorn installs its own handlers.
                # Clearing them sends its records to the root handler instead.
                **{
                    name: {"handlers": [], "propagate": True}
                    for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
                },
                # httpx logs every request URL at INFO, and a URL can hold a secret.
                # A Discord webhook URL is one example, because its token is part of the path.
                **{name: {"level": "WARNING"} for name in ("httpx", "httpcore")},
            },
        }
    )
