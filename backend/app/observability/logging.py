"""Structured JSON logging with correlation ids and mandatory secret redaction."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from app.utils.redact import redact, redact_text

CORRELATION_FIELDS = ("request_id", "session_id", "task_id", "plan_id", "step_id", "tool_call_id")

_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("saa_log_ctx", default=None)  # type: ignore[arg-type]

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def current_context() -> dict[str, str]:
    return dict(_ctx.get() or {})


@contextlib.contextmanager
def bind(**fields: str | None) -> Iterator[None]:
    """Bind correlation ids for the duration of the block (async-safe)."""
    merged = {**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        payload.update(_ctx.get() or {})
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx.get() or {}
        tag = " ".join(f"{k}={v}" for k, v in ctx.items() if k in ("task_id", "step_id"))
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        text = redact_text(record.getMessage())
        out = f"{base}{text}" + (f" [{tag}]" if tag else "")
        if record.exc_info:
            out += "\n" + redact_text(self.formatException(record.exc_info))
        return out


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access", "websockets"):
        logging.getLogger(noisy).setLevel(max(logging.getLogger(noisy).level, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"saa.{name}")
