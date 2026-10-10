"""Filesystem tools for multi-file workflows: ranked discovery, batch organization, batch reading.

Everything here is deterministic and runs without a model. A model chooses *what to look for* (terms);
code decides which files match, how sure it is, and what is moved. Every path goes through ``PathPolicy``
exactly as in the single-file tools, a whole batch is validated before the first file is touched, and
no tool here overwrites or deletes anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import Capability, RiskLevel
from app.models.tool import RiskAssessment
from app.security.paths import walk_confined
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.filesystem.tools import _TEXT_EXT, _read_pdf, _resolve, _stat_entry
from app.tools.schema import OutputModel
from app.utils.errors import AgentError, ErrorCode, PathViolation

_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv"}
_READABLE_EXT = _TEXT_EXT | {".pdf"}
MAX_SCANNED = 50_000
MAX_CONTENT_FILES = 150  # files whose content is opened per search
MAX_CONTENT_BYTES = 25 * 1024 * 1024  # larger files are matched by name only
STRONG_HITS = 3  # mentions of a primary term that make a document "about" it
_SKIP_CONTENT_CHARS = 200_000


def _term_re(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive: 'java' matches 'Java 17' and 'java_notes', not 'JavaScript'."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term.strip())}(?![A-Za-z0-9])", re.I)


def _count(patterns: list[re.Pattern[str]], text: str) -> int:
    return sum(len(p.findall(text)) for p in patterns)


def _parse_time(raw: str | None, field: str) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise AgentError(
            f"{field}: '{raw}' is not an ISO date (e.g. 2026-01-31)", code=ErrorCode.INVALID_INPUT
        ) from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _extract(path: Path, max_pages: int, max_chars: int) -> tuple[str | None, int | None]:
    """(text, pages) of a readable document, (None, None) when it cannot be read."""
    try:
        if path.suffix.lower() == ".pdf":
            text, pages = _read_pdf(path, f"1-{max_pages}", max_chars)
            return text, pages
        raw = path.read_bytes()[:max_chars]
        if b"\x00" in raw[:4096]:
            return None, None
        return raw.decode("utf-8", errors="replace"), None
    except (AgentError, OSError):
        return None, None


# ----------------------------------------------------------------------------- fs_find
class FsFindInput(BaseModel):
    root: str = Field(description="Folder to search under (recursive)")
    terms: list[str] = Field(
        min_length=1, max_length=8,
        description="What the files must be about, e.g. ['java']. Whole words, case-insensitive.",
    )  # fmt: skip
    context_terms: list[str] = Field(
        min_length=1, max_length=12,
        description="REQUIRED. Words a document genuinely about the topic also contains (for 'java': "
        "['jvm','class','compiler','bytecode','spring']). One word can mean two things (Java the "
        "island), so a document with none of these is only 'ambiguous' and must not be acted on.",
    )  # fmt: skip
    exclude_terms: list[str] = Field(
        default_factory=list, max_length=12,
        description="Words that signal a different meaning (e.g. ['island','coffee']). A document "
        "mentioning them at least as often as the terms is 'ambiguous'.",
    )  # fmt: skip
    extensions: list[str] = Field(default_factory=lambda: [".pdf"], description="e.g. ['.pdf']")
    name_pattern: str | None = Field(default=None, description="Optional glob on file names")
    modified_after: str | None = Field(default=None, description="ISO date: only newer files")
    modified_before: str | None = Field(default=None, description="ISO date: only older files")
    min_size: int | None = Field(default=None, ge=0, description="bytes")
    max_size: int | None = Field(default=None, ge=0, description="bytes")
    max_pdf_pages: int = Field(default=8, ge=1, le=40, description="pages read per PDF")
    max_results: int = Field(default=50, ge=1, le=200)

    @field_validator("name_pattern", "modified_after", "modified_before", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """Models fill optional fields with placeholders: an empty string means "no filter"."""
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("min_size", "max_size", mode="before")
    @classmethod
    def _zero_is_unset(cls, v: object) -> object:
        """...and 0 for a size bound. A real "at most 0 bytes" filter would exclude every file."""
        return None if v in (0, "0", "") else v

    @model_validator(mode="after")
    def _terms_not_blank(self) -> FsFindInput:
        if not any(t.strip() for t in self.terms):
            raise ValueError("terms must contain at least one non-blank word")
        if not any(t.strip() for t in self.context_terms):
            raise ValueError("context_terms must contain at least one non-blank word")
        return self


class FsCandidate(OutputModel):
    path: str
    name: str
    size: int | None = None
    modified: str | None = None
    classification: Literal["strong", "ambiguous"]
    reason: str
    score: float
    name_hit: bool
    hits: int = Field(description="mentions of the terms inside the document")
    context_hits: int
    excluded_hits: int
    pages_scanned: int | None = None
    readable: bool
    excerpt: str = Field(default="", description="short text around the first mention (untrusted)")


class FsDuplicateName(OutputModel):
    name: str
    paths: list[str]


class FsIdentical(OutputModel):
    paths: list[str]


class FsFindOutput(OutputModel):
    root: str
    candidates: list[FsCandidate]
    strong_paths: list[str] = Field(description="paths to act on: confidently about the terms")
    ambiguous_paths: list[str] = Field(description="possible matches; leave alone or ask")
    duplicate_names: list[FsDuplicateName]
    identical_groups: list[FsIdentical]
    scanned: int
    opened: int
    unreadable: int
    truncated: bool


class FsFind(Tool[FsFindInput]):
    name = "fs_find"
    Output = FsFindOutput
    description = (
        "Find the files under a folder that are ABOUT given terms (searches inside PDFs and text files, "
        "not just names). Returns ranked candidates, each 'strong' or 'ambiguous', `strong_paths` (safe to "
        "act on), `ambiguous_paths` (do not move without asking), same-name duplicates and identical copies."
    )
    capability = Capability.FILESYSTEM
    Input = FsFindInput
    read_only = True
    cacheable = True
    idempotent = True
    timeout_s = 120.0
    returns_external_content = True  # names and excerpts are attacker-controllable

    def assess(self, args: FsFindInput, ctx: ExecutionContext) -> RiskAssessment:
        rp = _resolve(ctx, args.root, must_exist=True)
        return RiskAssessment(
            risk=RiskLevel.LOW, action=f"Find files about {', '.join(args.terms)} in {rp.path}",
            resource=str(rp.path), category="file_read",
        )  # fmt: skip

    async def run(self, args: FsFindInput, ctx: ExecutionContext) -> ToolOutput:
        rp = _resolve(ctx, args.root, must_exist=True)
        if not rp.path.is_dir():
            raise AgentError(f"Not a directory: {rp.path}", code=ErrorCode.INVALID_INPUT)
        after = _parse_time(args.modified_after, "modified_after")
        before = _parse_time(args.modified_before, "modified_before")
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.extensions}
        primary = [_term_re(t) for t in args.terms if t.strip()]
        context = [_term_re(t) for t in args.context_terms if t.strip()]
        excluded = [_term_re(t) for t in args.exclude_terms if t.strip()]
        cap = min(args.max_results, ctx.settings.filesystem.max_search_results)
        max_chars = min(ctx.settings.filesystem.max_read_bytes, _SKIP_CONTENT_CHARS)

        scanned = opened = unreadable = 0
        truncated = False
        found: list[dict[str, Any]] = []
        for root, _dirs, names in walk_confined(rp.path, skip_dirs=_SKIP_DIRS, skip_hidden=True):
            for n in names:
                ctx.token.raise_if_cancelled()
                scanned += 1
                if scanned > MAX_SCANNED:
                    truncated = True
                    break
                p = Path(root, n)
                suffix = p.suffix.lower()
                if (exts and suffix not in exts) or (not exts and suffix not in _READABLE_EXT):
                    continue
                if args.name_pattern and not _glob(n, args.name_pattern):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not _passes_metadata(st, after, before, args):
                    continue
                try:
                    ctx.paths.resolve(
                        str(p), cwd=ctx.cwd
                    )  # credential-looking / protected: invisible
                except PathViolation:
                    continue
                name_hit = _count(primary, p.stem.replace("_", " ")) > 0
                text: str | None = None
                pages: int | None = None
                if (
                    st.st_size <= MAX_CONTENT_BYTES
                    and suffix in _READABLE_EXT
                    and opened < MAX_CONTENT_FILES
                ):
                    opened += 1
                    text, pages = await asyncio.to_thread(
                        _extract, p, args.max_pdf_pages, max_chars
                    )
                    if text is None:
                        unreadable += 1
                hits = _count(primary, text) if text else 0
                if hits == 0 and not name_hit:
                    continue
                found.append(
                    _score(
                        p,
                        st,
                        text,
                        pages,
                        name_hit=name_hit,
                        hits=hits,
                        context_hits=_count(context, text) if text else 0,
                        excluded_hits=_count(excluded, text) if text else 0,
                        primary=primary,
                    )  # fmt: skip
                )
            if truncated:
                break
        order = {"strong": 0, "ambiguous": 1}
        found.sort(key=lambda c: (order[c["classification"]], -c["score"], c["path"].lower()))
        if len(found) > cap:
            found, truncated = found[:cap], True
        strong = [c["path"] for c in found if c["classification"] == "strong"]
        ambiguous = [c["path"] for c in found if c["classification"] == "ambiguous"]
        by_name: dict[str, list[str]] = {}
        for c in found:
            by_name.setdefault(c["name"].lower(), []).append(c["path"])
        duplicate_names = [
            {"name": Path(v[0]).name, "paths": v} for v in by_name.values() if len(v) > 1
        ]
        identical = await asyncio.to_thread(_identical_groups, found)
        return ToolOutput(
            data={
                "root": str(rp.path), "candidates": found, "strong_paths": strong,
                "ambiguous_paths": ambiguous, "duplicate_names": duplicate_names,
                "identical_groups": identical, "scanned": scanned, "opened": opened,
                "unreadable": unreadable, "truncated": truncated,
            },
            summary=(
                f"{len(strong)} strong and {len(ambiguous)} ambiguous match(es) for "
                f"{', '.join(args.terms)} under {rp.path}"
            ),
        )  # fmt: skip


def _glob(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(name.lower(), pattern.lower())


def _passes_metadata(
    st: os.stat_result, after: float | None, before: float | None, a: FsFindInput
) -> bool:
    if after is not None and st.st_mtime < after:
        return False
    if before is not None and st.st_mtime > before:
        return False
    if a.min_size is not None and st.st_size < a.min_size:
        return False
    return not (a.max_size is not None and st.st_size > a.max_size)


def _score(
    p: Path, st: os.stat_result, text: str | None, pages: int | None, *, name_hit: bool, hits: int,
    context_hits: int, excluded_hits: int, primary: list[re.Pattern[str]],
) -> dict[str, Any]:  # fmt: skip
    readable = text is not None
    if not readable:
        cls, reason = "ambiguous", "the name matches but the content could not be read"
    elif excluded_hits and excluded_hits >= hits:
        cls = "ambiguous"
        reason = f"mentions the terms {hits}x but the excluded words appear {excluded_hits}x"
    elif context_hits == 0 and hits:
        cls, reason = "ambiguous", f"mentions the terms {hits}x but none of the confirming words"
    elif hits >= STRONG_HITS or (name_hit and hits >= 1):
        cls = "strong"
        reason = f"{hits} mention(s) in the text" + (" and in the file name" if name_hit else "")
    elif hits:
        cls, reason = "ambiguous", f"only {hits} mention(s) in the text (needs {STRONG_HITS})"
    else:
        cls, reason = "ambiguous", "the name matches but the text never mentions the terms"
    score = (3.0 if name_hit else 0.0) + min(hits, 20) * 1.0 + min(context_hits, 20) * 0.25
    score -= min(excluded_hits, 20) * 0.5
    excerpt = ""
    if text:
        for pat in primary:
            m = pat.search(text)
            if m:
                a = max(0, m.start() - 60)
                excerpt = " ".join(text[a : m.end() + 100].split())[:180]
                break
    return {
        "path": str(p), "name": p.name, "size": st.st_size,
        "modified": _stat_entry(p).get("modified"), "classification": cls, "reason": reason,
        "score": round(score, 2), "name_hit": name_hit, "hits": hits, "context_hits": context_hits,
        "excluded_hits": excluded_hits, "pages_scanned": pages, "readable": readable,
        "excerpt": excerpt,
    }  # fmt: skip


def _identical_groups(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hash: dict[str, list[str]] = {}
    for c in found:
        if c["size"] is None or c["size"] > MAX_CONTENT_BYTES:
            continue
        digest = _sha256(Path(c["path"]))
        if digest:
            by_hash.setdefault(digest, []).append(c["path"])
    return [{"paths": v} for v in by_hash.values() if len(v) > 1]


# ----------------------------------------------------------------------------- fs_organize
class FsOrganizeInput(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=100, description="Files to move or copy")
    dest_dir: str = Field(description="Destination folder (created if missing)")
    action: Literal["move", "copy"] = "move"
    on_conflict: Literal["skip", "rename"] = Field(
        default="skip",
        description="A different file with the same name is already there: leave the source alone "
        "('skip') or keep both ('rename' adds ' (2)'). Nothing is ever overwritten.",
    )


class FsMovedItem(OutputModel):
    src: str
    dst: str


class FsSkippedItem(OutputModel):
    src: str
    reason: str


class FsOrganizeOutput(OutputModel):
    dest_dir: str
    action: str
    moved: list[FsMovedItem]
    skipped: list[FsSkippedItem]
    already_in_place: list[str]
    dest_paths: list[str] = Field(
        description="where each requested file is now: moved, already there, or its identical twin"
    )
    moved_count: int
    skipped_count: int


class FsOrganizeArtifacts(OutputModel):
    dest_dir: str
    moved_count: int


def _unique_target(target: Path) -> Path:
    n = 2
    while True:
        cand = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _same_content(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and _sha256(a) == _sha256(b)
    except OSError:
        return False


def _organize_plan(
    ctx: ExecutionContext, args: FsOrganizeInput
) -> tuple[Path, list[tuple[Path, Path]], list[tuple[Path, str]], list[Path], dict[Path, Path]]:
    """Validate the WHOLE batch. Returns (dest, [(src, target)], [(src, skip reason)], [already done])."""
    move = args.action == "move"
    dest = _resolve(ctx, args.dest_dir, write=True, allow_root_itself=True).path
    if dest.exists() and not dest.is_dir():
        raise AgentError(f"{dest} exists and is not a folder", code=ErrorCode.ALREADY_EXISTS)
    todo: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, str]] = []
    done: list[Path] = []
    twins: dict[
        Path, Path
    ] = {}  # skipped source -> the identical file that stays in the destination
    missing: list[str] = []
    seen: set[str] = set()
    claimed: dict[str, Path] = {}  # target -> the source that will land there in this batch
    for raw in args.sources:
        try:
            src = _resolve(ctx, raw, must_exist=True, write=move, allow_root_itself=False).path
        except PathViolation as exc:
            if exc.code is ErrorCode.NOT_FOUND:
                lenient = _resolve(ctx, raw).path  # a re-run: it may already be in the destination
                if (dest / lenient.name).exists():
                    done.append(dest / lenient.name)
                    continue
                missing.append(str(lenient))
                continue
            raise
        key = os.path.normcase(str(src))
        if key in seen:
            continue
        seen.add(key)
        if not src.is_file():
            raise AgentError(
                f"{src} is not a file; only files can be organized", code=ErrorCode.INVALID_INPUT
            )
        if os.path.normcase(str(src.parent)) == os.path.normcase(str(dest)):
            done.append(src)
            continue
        target = _resolve(ctx, str(dest / src.name), write=True).path
        holder = target if target.exists() else claimed.get(os.path.normcase(str(target)))
        if holder is not None:
            if _same_content(src, holder):
                skipped.append((src, "an identical file is already in the destination"))
                twins[src] = target  # the identical file that will be/is in the destination
                continue
            if args.on_conflict == "skip":
                skipped.append(
                    (src, f"a different file named {src.name} is already in the destination")
                )
                continue
            target = _unique_target(target)
            while os.path.normcase(str(target)) in claimed:
                target = _unique_target(target)
        claimed[os.path.normcase(str(target))] = src
        todo.append((src, target))
    if missing:
        raise AgentError(
            "Not found (nothing was moved): " + ", ".join(missing[:5]), code=ErrorCode.NOT_FOUND,
            details={"missing": missing[:20]},
        )  # fmt: skip
    return dest, todo, skipped, done, twins


class FsOrganize(Tool[FsOrganizeInput]):
    name = "fs_organize"
    Output = FsOrganizeOutput
    Artifacts = FsOrganizeArtifacts
    description = (
        "Move (or copy) a LIST of files into one destination folder, creating it if needed. All paths are "
        "validated before the first file moves; nothing is overwritten or deleted; re-running is safe "
        "(files already in place are reported). Use it with `{{steps.sN.data.strong_paths}}` from fs_find."
    )
    capability = Capability.FILESYSTEM
    Input = FsOrganizeInput
    risk = RiskLevel.MEDIUM
    idempotent = True

    def assess(self, args: FsOrganizeInput, ctx: ExecutionContext) -> RiskAssessment:
        dest, todo, skipped, _done, _twins = _organize_plan(ctx, args)
        names = ", ".join(s.name for s, _ in todo[:5]) + (" …" if len(todo) > 5 else "")
        return RiskAssessment(
            risk=RiskLevel.MEDIUM,
            action=f"{args.action.capitalize()} {len(todo)} file(s) into {dest}: {names or '(nothing to do)'}",
            resource=str(dest),
            reasons=[f"{len(skipped)} file(s) will be left where they are"] if skipped else [],
            category="file_move" if args.action == "move" else "file_copy",
        )  # fmt: skip

    async def run(self, args: FsOrganizeInput, ctx: ExecutionContext) -> ToolOutput:
        dest, todo, skipped, done, twins = _organize_plan(ctx, args)
        moved: list[dict[str, str]] = []
        failed: list[str] = []
        if todo:
            dest.mkdir(parents=True, exist_ok=True)
        for src, target in todo:
            ctx.token.raise_if_cancelled()
            try:
                if target.exists():  # appeared since planning: never overwrite
                    skipped.append((src, "the destination file appeared while organizing"))
                    continue
                if args.action == "move":
                    await asyncio.to_thread(shutil.move, str(src), str(target))
                else:
                    await asyncio.to_thread(shutil.copy2, src, target)
                moved.append({"src": str(src), "dst": str(target)})
            except OSError as exc:
                failed.append(f"{src.name}: {exc.strerror or type(exc).__name__}")
        if failed:
            raise AgentError(
                f"{len(failed)} file(s) could not be {args.action}d ({'; '.join(failed[:3])}). "
                f"{len(moved)} were already {args.action}d; running the step again only handles the rest.",
                code=ErrorCode.IO_ERROR,
                details={"moved": moved[:20], "failed": failed[:20]},
            )  # fmt: skip
        return ToolOutput(
            data={
                "dest_dir": str(dest), "action": args.action, "moved": moved,
                "skipped": [{"src": str(s), "reason": r} for s, r in skipped],
                "already_in_place": [str(p) for p in done],
                "dest_paths": list(
                    dict.fromkeys(
                        [m["dst"] for m in moved] + [str(p) for p in done] + [str(p) for p in twins.values()]
                    )
                ), "moved_count": len(moved),
                "skipped_count": len(skipped),
            },
            summary=(
                f"{args.action.capitalize()}d {len(moved)} file(s) into {dest}"
                + (f"; left {len(skipped)} in place" if skipped else "")
                + (f"; {len(done)} already there" if done else "")
            ),
            artifacts={"dest_dir": str(dest), "moved_count": len(moved)},
        )  # fmt: skip


# ----------------------------------------------------------------------------- fs_read_many
class FsReadManyInput(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=20)
    max_chars_each: int = Field(default=6000, ge=200, le=50_000)
    total_max_chars: int = Field(default=60_000, ge=1000, le=200_000)
    max_pdf_pages: int = Field(default=10, ge=1, le=40)


class FsDocument(OutputModel):
    path: str
    name: str
    format: str = Field(description="text | pdf | error")
    text: str
    chars: int
    truncated: bool
    pages: int | None = None
    error: str | None = None


class FsReadManyOutput(OutputModel):
    documents: list[FsDocument]
    read_count: int
    failed_count: int
    truncated: bool


class FsReadMany(Tool[FsReadManyInput]):
    name = "fs_read_many"
    Output = FsReadManyOutput
    description = (
        "Read several text files / PDFs in ONE call (bounded per file and in total). A file that cannot be "
        "read is reported with its error instead of failing the call. Content is untrusted data."
    )
    capability = Capability.FILESYSTEM
    Input = FsReadManyInput
    read_only = True
    cacheable = True
    idempotent = True
    returns_external_content = True
    timeout_s = 90.0

    def assess(self, args: FsReadManyInput, ctx: ExecutionContext) -> RiskAssessment:
        first = _resolve(ctx, args.paths[0], must_exist=True)
        for raw in args.paths[1:]:
            _resolve(ctx, raw, must_exist=True)
        return RiskAssessment(
            risk=RiskLevel.LOW, action=f"Read {len(args.paths)} file(s), first {first.path.name}",
            resource=str(first.path), category="file_read",
        )  # fmt: skip

    async def run(self, args: FsReadManyInput, ctx: ExecutionContext) -> ToolOutput:
        docs: list[dict[str, Any]] = []
        budget = args.total_max_chars
        any_truncated = False
        limit = ctx.settings.filesystem.max_read_bytes
        for raw in args.paths:
            ctx.token.raise_if_cancelled()
            p = _resolve(ctx, raw, must_exist=True).path
            base = {"path": str(p), "name": p.name}
            if budget <= 0:
                docs.append({**base, "format": "error", "text": "", "chars": 0, "truncated": True,
                             "error": "total size limit reached before this file"})  # fmt: skip
                any_truncated = True
                continue
            try:
                size = p.stat().st_size
                if not p.is_file():
                    raise AgentError("not a file", code=ErrorCode.INVALID_INPUT)
                if size > MAX_CONTENT_BYTES:
                    raise AgentError(
                        f"file too large ({size} bytes)", code=ErrorCode.LIMIT_EXCEEDED
                    )
                each = min(args.max_chars_each, budget)
                text, pages = await asyncio.to_thread(
                    _extract, p, args.max_pdf_pages, max(each, 1) * 2
                )
                if text is None:
                    raise AgentError("could not be read as text", code=ErrorCode.IO_ERROR)
                is_pdf = p.suffix.lower() == ".pdf"
                clipped = len(text) > each or (not is_pdf and size > limit)
                text = text[:each]
                budget -= len(text)
                any_truncated = any_truncated or clipped
                docs.append({**base, "format": "pdf" if is_pdf else "text", "text": text,
                             "chars": len(text), "truncated": clipped, "pages": pages, "error": None})  # fmt: skip
            except AgentError as exc:
                docs.append({**base, "format": "error", "text": "", "chars": 0, "truncated": False,
                             "error": exc.message})  # fmt: skip
        read = sum(1 for d in docs if d["error"] is None)
        if read == 0:
            raise AgentError(
                "None of the files could be read: " + "; ".join(f"{d['name']}: {d['error']}" for d in docs[:3]),
                code=ErrorCode.IO_ERROR, details={"errors": [d["error"] for d in docs[:10]]},
            )  # fmt: skip
        return ToolOutput(
            data={"documents": docs, "read_count": read, "failed_count": len(docs) - read,
                  "truncated": any_truncated},
            summary=f"Read {read} of {len(docs)} file(s)" + (" (some truncated)" if any_truncated else ""),
        )  # fmt: skip


WORKFLOW_TOOLS: list[type[Tool[Any]]] = [FsFind, FsOrganize, FsReadMany]
