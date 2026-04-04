"""
logging_config.py — Structured JSON logging setup.

Produces one JSON line per log record — easy to ingest into Datadog,
Papertrail, Logtail, or any log aggregator. Falls back to a readable
human format when LOG_LEVEL is DEBUG for local development.
"""

import logging
import sys
from app.config import settings


class _JSONFormatter(logging.Formatter):
    """Minimal structured-log formatter (no external lib required)."""

    import json as _json

    def format(self, record: logging.LogRecord) -> str:
        import json, traceback
        data = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = traceback.format_exception(*record.exc_info)
        # Attach any extra kwargs passed via `extra={...}`
        for key, val in record.__dict__.items():
            if key not in (
                "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "name",
                "message",
            ):
                data[key] = val
        return json.dumps(data)


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)

    if level == logging.DEBUG:
        # Human-readable format for local dev
        fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
    else:
        handler.setFormatter(_JSONFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quieten noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
