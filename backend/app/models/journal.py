"""The file journal (NEXT-D): every change a task makes to files, and how to take it back.

An entry is written *before* the change (``PENDING``) and settled after it from the observed state.
A rollback walks the entries newest first and reverses each one only while the item is still exactly
as the agent left it. Details: ``docs/file-transactions.md``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.utils.ids import new_id, utcnow


class JournalOp(StrEnum):
    CREATE_FILE = "create_file"  # nothing was at `path`; the agent wrote a file there
    REPLACE_FILE = "replace_file"  # a file was at `path`; its previous content is in `backup`
    CREATE_DIR = "create_dir"  # nothing was at `path`; the agent created a folder
    MOVE = "move"  # `src` was moved to `path`; what `path` held before (overwrite) is in `backup`
    COPY = "copy"  # `src` was copied to `path`; what `path` held before (overwrite) is in `backup`
    RECYCLE = "recycle"  # `path` was sent to the Recycle Bin


class JournalState(StrEnum):
    PENDING = "pending"  # recorded before the change; whether it happened is decided by looking
    APPLIED = "applied"  # the change happened and was verified
    FAILED = "failed"  # the change did not happen; nothing to undo
    UNDOING = "undoing"  # recorded before the undo; a rollback that stopped here continues from it
    UNDONE = "undone"  # the item is back as it was before the change
    SKIPPED = "skipped"  # left as it is (changed since, never took effect, or not determinable)
    MANUAL = "manual"  # the agent cannot undo it; `detail` says what the user can do


# Entries a rollback still has to look at.
OPEN_STATES = frozenset({JournalState.PENDING, JournalState.APPLIED, JournalState.UNDOING})


class JournalEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("fj"))
    task_id: str
    tool_call_id: str | None = None
    seq: int = 0  # order of the changes within the task
    op: JournalOp
    state: JournalState = JournalState.PENDING
    kind: Literal["file", "dir"] = "file"
    path: str  # the location the change affected (for a move or copy: where the item went)
    src: str | None = None  # move/copy: where the item came from
    # What the agent left at `path` (move: what was at `src`). Compared before anything is undone.
    fingerprint: str | None = None
    backup: str | None = None  # where the previous content of `path` is kept
    backup_fingerprint: str | None = None
    detail: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
