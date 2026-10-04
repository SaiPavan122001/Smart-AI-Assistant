"""Identifier and clock helpers (single place so tests can reason about them)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id(prefix: str = "") -> str:
    """Return a short, sortable-enough unique id such as ``task_3f9a1c2b7d4e``."""
    suffix = uuid.uuid4().hex[:16]
    return f"{prefix}_{suffix}" if prefix else suffix


def utcnow() -> datetime:
    return datetime.now(UTC)
