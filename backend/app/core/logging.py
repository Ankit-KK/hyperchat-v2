"""Structured (JSON-lines) logging to stdout, collected by journald on the VPS.

Request logs record method, path, status and duration only. Query strings, headers and
bodies are never logged: OBS URLs carry tokens in the query string, and dashboard
requests carry session tokens in headers.
"""

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Route uvicorn's own loggers through the same JSON handler; access logs come from our middleware.
    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers[:] = []
        uv.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request URL at INFO; Pusher REST URLs carry auth query parameters. Warnings only.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
