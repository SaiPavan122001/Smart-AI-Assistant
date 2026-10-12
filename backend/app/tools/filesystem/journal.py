"""Transactional file changes (NEXT-D): journal, staged and verified writes, undo.

* Every change is journalled **before** it happens (``FileJournal.change``). If the entry cannot be
  written, the change is not made. Afterwards the entry is settled from the observed state, never
  from what the code meant to do.
* New content is staged next to its destination, verified, and put in place with one rename. What a
  change replaces is first preserved in the task's backup folder: nothing the agent overwrites is lost.
* ``undo_entry`` reverses one entry only while the item is still exactly as the agent left it. It runs
  in small steps that each check their own precondition, so a rollback that stopped half-way (crash,
  STOP, error) continues where it was and never does a step twice.

Details: ``docs/file-transactions.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.models.journal import OPEN_STATES, JournalEntry, JournalOp, JournalState
from app.observability.logging import get_logger
from app.security.paths import is_redirect, walk_confined
from app.state.repository import Repository
from app.utils.errors import AgentError, ErrorCode
from app.utils.ids import utcnow
from app.utils.redact import redact_text

if TYPE_CHECKING:
    from app.tools.context import ExecutionContext

log = get_logger("journal")

HASH_LIMIT = 64 * 1024 * 1024  # larger files are fingerprinted by size and modification time
TREE_LIMIT = 5000  # a folder with more files has no fingerprint: it is not undone automatically
STAGING_SUFFIX = ".saa-tmp"
_ERROR_NOT_SAME_DEVICE = 17

Observed = Literal["after", "before", "unknown"]


# ----------------------------------------------------------------------------- fingerprints
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_fingerprint(data: bytes) -> str | None:
    """The fingerprint a file with exactly these bytes will have (None above ``HASH_LIMIT``)."""
    return None if len(data) > HASH_LIMIT else "sha256:" + hashlib.sha256(data).hexdigest()


def _file_fingerprint(p: Path) -> str:
    st = p.stat()
    if st.st_size > HASH_LIMIT:
        return f"sig:{st.st_size}:{st.st_mtime_ns}"
    return "sha256:" + _sha256(p)


def _tree_lines(p: Path, *, content: bool, limit: int | None) -> list[str] | None:
    lines: list[str] = []
    files = 0
    for root, dirs, names in walk_confined(p):
        rel = os.path.relpath(root, p).replace(os.sep, "/")
        lines += [f"D {rel}/{d}" for d in dirs]
        for name in names:
            files += 1
            if limit is not None and files > limit:
                return None
            f = Path(root) / name
            detail = _file_fingerprint(f) if content else str(f.stat().st_size)
            lines.append(f"F {rel}/{name} {detail}")
    return sorted(lines)


def fingerprint(p: Path) -> str | None:
    """What is at ``p`` now: the content hash of a file (size and modification time above
    ``HASH_LIMIT``), a hash over a folder's relative paths and file fingerprints, or None when
    nothing is there, a redirect is there, or the folder is too large to fingerprint."""
    try:
        if is_redirect(p) or not p.exists():
            return None
        if p.is_file():
            return _file_fingerprint(p)
        if p.is_dir():
            lines = _tree_lines(p, content=True, limit=TREE_LIMIT)
            if lines is None:
                return None
            return "tree:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()
    except OSError:
        return None
    return None


def _same(a: Path, b: Path) -> bool:
    """Is ``b`` a faithful copy of ``a``? Files: size and content (size only above ``HASH_LIMIT``).
    Folders: the same relative paths and sizes, and the same fingerprint where one can be computed."""
    if a.is_dir():
        if not b.is_dir():
            return False
        if _tree_lines(a, content=False, limit=None) != _tree_lines(b, content=False, limit=None):
            return False
        fa, fb = fingerprint(a), fingerprint(b)
        return fa == fb
    if not b.is_file() or a.stat().st_size != b.stat().st_size:
        return False
    return a.stat().st_size > HASH_LIMIT or _sha256(a) == _sha256(b)


# ----------------------------------------------------------------------------- staged operations
def staging_path(target: Path) -> Path:
    return target.with_name(target.name + STAGING_SUFFIX)


def _remove(p: Path) -> None:
    """Remove the agent's own scratch item (a staging copy or a backup), never a user item."""
    if is_redirect(p):
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def write_staged(target: Path, data: bytes) -> None:
    """Write ``data`` to a staging file next to ``target``, verify it, then replace ``target`` with
    one rename. ``target`` is either the old file or the new one, never half-written."""
    tmp = staging_path(target)
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if _sha256(tmp) != hashlib.sha256(data).hexdigest():
            raise AgentError(
                f"The staged content of {target.name} does not match; nothing was replaced",
                code=ErrorCode.VERIFICATION_FAILED,
            )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def copy_verified(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` (which must not exist): copied to a staging name next to ``dst``,
    compared with the source, then renamed into place. A failed copy leaves nothing at ``dst``."""
    tmp = staging_path(dst)
    if os.path.lexists(tmp):
        _remove(tmp)  # left behind by an interrupted run
    try:
        if src.is_dir():
            # A symlink or junction inside the folder is not copied: its target may be anywhere.
            shutil.copytree(
                src, tmp, symlinks=True,
                ignore=lambda folder, names: [n for n in names if is_redirect(os.path.join(folder, n))],
            )  # fmt: skip
        else:
            shutil.copy2(src, tmp)
        if not _same(src, tmp):
            raise AgentError(
                f"The copy of {src.name} does not match the original; nothing was put in place",
                code=ErrorCode.VERIFICATION_FAILED,
            )
        os.rename(tmp, dst)  # refuses an existing destination
    except BaseException:
        if os.path.lexists(tmp):
            _remove(tmp)
        raise


def _cross_device(exc: OSError) -> bool:
    import errno

    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _ERROR_NOT_SAME_DEVICE


def move_verified(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` (which must not exist). One rename on the same volume; across volumes
    a verified copy is put in place first and only then is the source removed."""
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if not _cross_device(exc):
            raise
    copy_verified(src, dst)
    if src.is_dir() and not is_redirect(src):
        shutil.rmtree(src)
    else:
        src.unlink()


def _recycle(p: Path) -> None:
    from send2trash import send2trash

    try:
        send2trash(str(p))
    except Exception as exc:  # no permanent-delete fallback by design
        raise AgentError(
            f"Could not move {p.name} to the Recycle Bin ({type(exc).__name__}); it was left in place",
            code=ErrorCode.IO_ERROR,
        ) from exc


# ----------------------------------------------------------------------------- observation
def observe(e: JournalEntry) -> Observed:
    """Does the filesystem show the state *after* this change, the state *before* it, or neither?
    Read-only."""
    p = Path(e.path)
    there = os.path.lexists(p)
    if e.op is JournalOp.RECYCLE:
        return "before" if there else "after"
    if e.op is JournalOp.CREATE_DIR:
        if p.is_dir() and not is_redirect(p):
            return "after"
        return "unknown" if there else "before"
    now = fingerprint(p)
    if e.fingerprint is None:  # too large to fingerprint: only presence can be observed
        after = there and (e.kind != "dir" or p.is_dir())
    else:
        after = now == e.fingerprint
    prior = (now == e.backup_fingerprint) if e.backup_fingerprint else not there
    if e.op is JournalOp.MOVE:
        src = Path(e.src or "")
        src_there = os.path.lexists(src)
        if after and not src_there:
            return "after"
        src_now = fingerprint(src)
        if prior and src_there and (e.fingerprint is None or src_now == e.fingerprint):
            return "before"
        return "unknown"
    if after:
        return "after"
    return "before" if prior else "unknown"


def describe(e: JournalEntry) -> str:
    """What undoing this entry does, in words the user can check."""
    name = Path(e.path).name
    match e.op:
        case JournalOp.CREATE_FILE:
            return f"Remove the file the agent created at {e.path} (to the Recycle Bin)"
        case JournalOp.REPLACE_FILE:
            return f"Restore the previous content of {e.path} (the agent's version goes to the Recycle Bin)"
        case JournalOp.CREATE_DIR:
            return f"Remove the folder the agent created at {e.path} if it is empty"
        case JournalOp.MOVE:
            back = " and put back what it replaced" if e.backup else ""
            return f"Move {name} back from {e.path} to {e.src}{back}"
        case JournalOp.COPY:
            back = " and put back what it replaced" if e.backup else ""
            return f"Remove the copy at {e.path} (to the Recycle Bin){back}"
        case JournalOp.RECYCLE:
            return f"{e.path} was sent to the Recycle Bin: it can only be restored from there"
    return e.op.value


# ----------------------------------------------------------------------------- undo
def _restore_backup(e: JournalEntry) -> None:
    if e.backup is None:
        return
    p, backup = Path(e.path), Path(e.backup)
    if not os.path.lexists(p) and os.path.lexists(backup):
        p.parent.mkdir(parents=True, exist_ok=True)
        move_verified(backup, p)


def _drop_backup(e: JournalEntry) -> None:
    """The item is as it was before the change: the agent's backup copy, if any is left, is not needed."""
    if e.backup is None:
        return
    backup = Path(e.backup)
    if os.path.lexists(backup):
        _remove(backup)
    try:
        backup.parent.rmdir()  # the entry's own folder, when empty
    except OSError:
        pass


def undo_entry(e: JournalEntry, previous: JournalState) -> tuple[JournalState, str]:
    """Reverse one change if the item is still as the agent left it. Every step checks its own
    precondition, so running this again after an interruption only does what is left.

    ``previous`` is the state before this undo began: an ``APPLIED`` change is only undone from the
    exact state the agent left; a ``PENDING`` change (or an undo that was interrupted) may be found
    half-way and is completed from there."""
    p = Path(e.path)
    if os.path.lexists(staging_path(p)):
        _remove(staging_path(p))  # left behind by an interrupted change
    if e.op is JournalOp.RECYCLE:
        return (
            JournalState.MANUAL,
            f"{p.name} is in the Recycle Bin; restore it from there to get it back",
        )
    if e.fingerprint is None and e.op is not JournalOp.CREATE_DIR:
        return JournalState.MANUAL, (
            f"{p.name} was too large to fingerprint, so the agent cannot prove it is unchanged: "
            + describe(e).lower()
            + " yourself if you want it undone"
        )
    seen = observe(e)
    if seen == "before":
        _drop_backup(e)
        if previous is JournalState.UNDOING:
            return JournalState.UNDONE, "back as it was before the change"
        if previous is JournalState.PENDING:
            return JournalState.SKIPPED, "the change never took effect; nothing to undo"
        return JournalState.SKIPPED, "already back as it was before the change; nothing was done"
    if previous is JournalState.APPLIED and seen != "after":
        return JournalState.SKIPPED, "changed since the agent made the change; left as it is"

    match e.op:
        case JournalOp.CREATE_FILE | JournalOp.COPY:
            if fingerprint(p) == e.fingerprint:
                _recycle(p)
            _restore_backup(e)
        case JournalOp.REPLACE_FILE:
            if fingerprint(p) == e.fingerprint:
                _recycle(p)
            _restore_backup(e)
        case JournalOp.CREATE_DIR:
            if p.is_dir() and not is_redirect(p):
                if any(p.iterdir()):
                    return JournalState.SKIPPED, (
                        "the folder is not empty (it holds items the agent did not create or that changed); left as it is"
                    )
                p.rmdir()
        case JournalOp.MOVE:
            src = Path(e.src or "")
            if fingerprint(p) == e.fingerprint and not os.path.lexists(src):
                src.parent.mkdir(parents=True, exist_ok=True)
                move_verified(p, src)
            _restore_backup(e)

    if observe(e) == "before":
        _drop_backup(e)
        return JournalState.UNDONE, "back as it was before the change"
    return JournalState.SKIPPED, "the item is not as the agent left it; left as it is"


# ----------------------------------------------------------------------------- the journal
class FileJournal:
    """Writes and settles journal entries for the filesystem tools and undoes them for a rollback.

    Backups live under ``<root>/<task_id>/<entry_id>/`` (``root`` is ``<data_dir>/journal``)."""

    def __init__(self, repo: Repository, root: Path) -> None:
        self._repo = repo
        self.root = root
        self._next_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _seq(self, task_id: str) -> int:
        async with self._lock:
            if task_id not in self._next_seq:
                entries = await self._repo.list_journal(task_id)
                self._next_seq[task_id] = max((x.seq for x in entries), default=0) + 1
            seq = self._next_seq[task_id]
            self._next_seq[task_id] = seq + 1
            return seq

    async def entries(self, task_id: str) -> list[JournalEntry]:
        return await self._repo.list_journal(task_id)

    async def get(self, task_id: str, entry_id: str) -> JournalEntry | None:
        return next((x for x in await self.entries(task_id) if x.id == entry_id), None)

    async def _save(self, e: JournalEntry, state: JournalState, detail: str | None = None) -> None:
        e.state = state
        if detail is not None:
            e.detail = redact_text(detail)  # may carry an OS error message; it leaves the process
        e.updated_at = utcnow()
        await self._repo.save_journal_entry(e)

    async def make_dirs(self, ctx: ExecutionContext, folder: Path) -> None:
        """Create ``folder`` and any missing parents, one journalled change per folder created."""
        missing: list[Path] = []
        d = folder
        while not os.path.lexists(d) and d.parent != d:
            missing.append(d)
            d = d.parent
        for d in reversed(missing):
            async with self.change(ctx, JournalOp.CREATE_DIR, d, kind="dir"):
                d.mkdir()

    @asynccontextmanager
    async def change(
        self, ctx: ExecutionContext, op: JournalOp, path: Path, *, kind: Literal["file", "dir"] = "file",
        src: Path | None = None, fingerprint_after: str | None = None, preserve: bool = False,
    ) -> AsyncIterator[JournalEntry]:  # fmt: skip
        """Journal one change around the code that makes it.

        The entry is stored before the body runs; if that fails the body never runs. With
        ``preserve`` the entry gets a backup location for what is at ``path`` now (the body moves or
        copies it there). Afterwards the entry is settled from what is observed: ``APPLIED`` only if the
        expected result is there (otherwise the change fails as unverified), ``FAILED`` if nothing
        changed, ``PENDING`` with the reason if the state cannot be told."""
        e = JournalEntry(
            task_id=ctx.task_id, tool_call_id=ctx.tool_call_id, seq=await self._seq(ctx.task_id), op=op,
            kind=kind, path=str(path), src=str(src) if src is not None else None, fingerprint=fingerprint_after,
        )  # fmt: skip
        if preserve:
            e.backup = str(self.root / ctx.task_id / e.id / path.name)
            e.backup_fingerprint = fingerprint(path)
        try:
            await self._repo.save_journal_entry(e)
        except Exception as exc:
            log.exception("failed to journal a file change", extra={"op": op.value})
            raise AgentError(
                "The change was NOT made: it could not be recorded in the file journal, so it could not "
                "be undone or recovered.",
                code=ErrorCode.INTERNAL,
            ) from exc
        try:
            yield e
        except BaseException as exc:
            await self._settle(e, failure=exc)
            raise
        await self._settle(e)
        if e.state is not JournalState.APPLIED:
            raise AgentError(
                f"The change to {path.name} could not be verified afterwards ({e.detail})",
                code=ErrorCode.VERIFICATION_FAILED,
            )

    async def _settle(self, e: JournalEntry, failure: BaseException | None = None) -> None:
        try:
            seen = await asyncio.to_thread(observe, e)
        except Exception:
            seen = "unknown"
        why = f"{type(failure).__name__}" if failure is not None else ""
        if seen == "after":
            state, detail = JournalState.APPLIED, ""
        elif seen == "before":
            state = JournalState.FAILED
            detail = f"nothing was changed ({why})" if why else "the expected result is not there"
            try:  # the original is still in place: a copy taken for undo is not needed
                await asyncio.to_thread(_drop_backup, e)
            except OSError:
                log.warning("could not remove an unused journal backup", extra={"entry": e.id})
        else:
            state = JournalState.PENDING
            detail = "the result could not be confirmed" + (f" after {why}" if why else "")
        try:
            await self._save(e, state, detail)
        except Exception:  # the entry stays PENDING in storage; a rollback looks at the files again
            log.exception("failed to settle a journal entry", extra={"entry": e.id})

    async def undo(self, e: JournalEntry) -> JournalEntry:
        """Undo one entry (write-ahead: ``UNDOING`` is stored before anything is touched)."""
        if e.state not in OPEN_STATES:
            return e
        previous = e.state
        await self._save(e, JournalState.UNDOING)
        try:
            state, detail = await asyncio.to_thread(undo_entry, e, previous)
        except AgentError as exc:
            await self._save(e, JournalState.UNDOING, f"the undo did not complete: {exc.message}")
            raise
        except OSError as exc:
            await self._save(
                e,
                JournalState.UNDOING,
                f"the undo did not complete: {exc.strerror or type(exc).__name__}",
            )
            raise AgentError(
                f"Undo of {Path(e.path).name} did not complete ({exc.strerror or type(exc).__name__}); "
                "running the rollback again continues from here",
                code=ErrorCode.IO_ERROR,
            ) from exc
        await self._save(e, state, detail)
        return e
