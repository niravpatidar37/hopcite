"""Structured JSON logs on stdout (Cloud Logging parses `severity`, `message` and
`logging.googleapis.com/trace`), with PII redaction applied to the message and every string field.

    log = get_logger("kgrag.api")
    log.info("ask", extra={"fields": {"route": "graph", "latency_ms": 812.4}})
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from kgrag.redact import redact_pii

_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}
_configured = False


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return redact_pii(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "logger": record.name,
            "message": redact_pii(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                if k not in out:
                    out[k] = _scrub(v)
        trace = getattr(record, "trace", None)
        if trace:
            out["logging.googleapis.com/trace"] = str(trace)
        if record.exc_info:
            # Exception type only: messages and tracebacks can carry user text or connection strings.
            exc_type = record.exc_info[0]
            out["exception"] = exc_type.__name__ if exc_type else "Exception"
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging(fmt: str | None = None, level: int = logging.INFO) -> None:
    """Route the root logger (and uvicorn's loggers) through one stdout handler. Idempotent."""
    global _configured
    if _configured:
        return
    if fmt is None:
        from kgrag.config import get_settings

        fmt = get_settings().log_format
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # The neo4j driver logs notifications per query at INFO; keep warnings and up.
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    # Schema-migration reads of not-yet-existing properties raise harmless "unknown property" notices.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
