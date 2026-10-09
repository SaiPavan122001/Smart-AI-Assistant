"""Evaluate the user's explicit goal against the real final state.

Nothing here trusts a plan or a model: a file counts if it exists on disk with content, a URL counts as a
source only if a tool returned it during this task, and a summary counts as grounded only if its
vocabulary comes from text a tool returned. A requirement that cannot be decided is ``unverified``, never
``met``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from app.models.goal import (
    GoalSpec,
    Requirement,
    RequirementKind,
    RequirementResult,
    RequirementStatus,
)
from app.models.verification import VerificationResult
from app.tools.context import ExecutionContext
from app.tools.observed import urls_in
from app.utils.errors import PathViolation

MIN_GROUNDED_RATIO = 0.55  # share of a summary's distinctive words that appear in what was read
MIN_SUMMARY_WORDS = 5
_MAX_ARTIFACT_CHARS = 400_000
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]{3,}")
_URL_TEXT = re.compile(r"https?://\S+", re.I)
_STOP = frozenset(
    """
    about above after again also another because been before being between both cannot could does doing
    each either else from further have having here however into itself just like made make many more most
    much must only other over same several should since some such than that their them then there these
    they this those through under until upon very were what when where which while will with within would
    your document documents file files summary summaries summarize summarizes summarised overview section
    sections following include includes including included based main points key topic topics discusses
    discussed covers covered describes described provides provided explains explained introduction
    conclusion note notes page pages source sources title url urls link links resource resources
    comparison compare compared differences difference similarities saved read
    """.split()  # noqa: SIM905
)


def _met(req: Requirement, evidence: str) -> RequirementResult:
    return RequirementResult(
        id=req.id,
        kind=req.kind.value,
        text=req.text,
        status=RequirementStatus.MET,
        evidence=evidence,
    )


def _unmet(req: Requirement, evidence: str) -> RequirementResult:
    return RequirementResult(
        id=req.id,
        kind=req.kind.value,
        text=req.text,
        status=RequirementStatus.UNMET,
        evidence=evidence,
    )


def _unverified(req: Requirement, evidence: str) -> RequirementResult:
    return RequirementResult(
        id=req.id, kind=req.kind.value, text=req.text, status=RequirementStatus.UNVERIFIED,
        evidence=evidence,
    )  # fmt: skip


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_URL_TEXT.sub(" ", text).lower()) if w not in _STOP}


def _find_by_name(ctx: ExecutionContext, name: str, since: float, search: bool) -> Path | None:
    """A file with this name written during the task: tracked writes first, then a bounded search of
    the writable roots (covers files saved through a desktop application)."""
    want = name.lower()
    for raw in reversed(ctx.observed.written):
        p = Path(raw)
        if p.name.lower() == want and p.is_file():
            return p
    if not search:
        return None
    scanned = 0
    for root in ctx.paths.writable_roots:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d not in {"node_modules", ".venv"}
            ]
            for f in files:
                scanned += 1
                if scanned > 30_000:
                    return None
                if f.lower() == want:
                    p = Path(cur, f)
                    try:
                        if p.stat().st_mtime >= since - 2:
                            return p
                    except OSError:
                        continue
    return None


def _artifact_file(
    ctx: ExecutionContext, req: Requirement | None, since: float, search: bool = True
) -> tuple[Path | None, str]:
    """(file, problem). ``req`` None: the most recent file the task wrote."""
    if req is not None and req.path:
        try:
            p = ctx.paths.resolve(req.path, cwd=ctx.cwd).path
        except PathViolation as exc:
            return None, f"cannot check {req.path}: {exc.message}"
        if not p.is_file():
            return None, f"{req.path} does not exist"
        if str(p) not in ctx.observed.written and p.stat().st_mtime < since - 2:
            return None, f"{p.name} exists but was not written by this task"
        return p, ""
    if req is not None and req.name:
        found = _find_by_name(ctx, req.name, since, search)
        return (found, "") if found else (None, f"no file named {req.name} was saved by this task")
    for raw in reversed(ctx.observed.written):
        if Path(raw).is_file():
            return Path(raw), ""
    return None, "no file was saved by this task"


def _read(p: Path) -> str:
    return p.read_bytes()[:_MAX_ARTIFACT_CHARS].decode("utf-8", errors="replace")


def _artifact_req(goal: GoalSpec) -> Requirement | None:
    return next((r for r in goal.requirements if r.kind is RequirementKind.ARTIFACT), None)


def evaluate_goal(
    goal: GoalSpec, ctx: ExecutionContext, *, answer: str | None, since: float | None = None,
    search: bool = True,
) -> list[RequirementResult]:  # fmt: skip
    since = since if since is not None else time.time() - 3600
    out: list[RequirementResult] = []
    for req in goal.requirements:
        if req.kind is RequirementKind.ARTIFACT:
            f, problem = _artifact_file(ctx, req, since, search)
            if f is None:
                out.append(_unmet(req, problem))
            elif f.stat().st_size == 0:
                out.append(_unmet(req, f"{f.name} was created but is empty"))
            else:
                out.append(_met(req, f"{f} ({f.stat().st_size} bytes)"))
        elif req.kind is RequirementKind.MIN_URLS:
            out.append(_check_urls(req, goal, ctx, answer, since, search))
        elif req.kind is RequirementKind.GROUNDED_SUMMARY:
            out.append(_check_grounded(req, goal, ctx, since, search))
    return out


def _check_urls(
    req: Requirement, goal: GoalSpec, ctx: ExecutionContext, answer: str | None, since: float,
    search: bool,
) -> RequirementResult:  # fmt: skip
    wanted = req.count or 1
    if req.where == "artifact":
        f, problem = _artifact_file(ctx, _artifact_req(goal), since, search)
        if f is None:
            return _unmet(req, problem)
        text = _read(f)
        where = f.name
    else:
        if not answer:
            return _unverified(req, "there is no answer text to check")
        text, where = answer, "the answer"
    found = list(dict.fromkeys(urls_in(text)))
    invented = [u for u in found if u not in ctx.observed.urls]
    grounded = [u for u in found if u in ctx.observed.urls]
    if invented:
        return _unmet(
            req, f"{len(invented)} URL(s) in {where} were never returned by any tool: " + ", ".join(invented[:3])
        )  # fmt: skip
    if len(grounded) < wanted:
        return _unmet(
            req, f"{where} has {len(grounded)} distinct source URL(s); {wanted} were requested"
        )
    return _met(req, f"{len(grounded)} distinct source URLs in {where}, all returned by tools")


def _check_grounded(
    req: Requirement, goal: GoalSpec, ctx: ExecutionContext, since: float, search: bool
) -> RequirementResult:
    f, problem = _artifact_file(ctx, _artifact_req(goal), since, search)
    if f is None:
        return _unmet(req, problem)
    if not ctx.observed.texts:
        return _unmet(req, "the summary was saved, but nothing was read to summarise")
    words = content_words(_read(f))
    if len(words) < MIN_SUMMARY_WORDS:
        return _unmet(
            req, f"{f.name} is too short to be a summary ({len(words)} distinctive words)"
        )
    seen = content_words(" ".join(ctx.observed.texts))
    ratio = len(words & seen) / len(words)
    if ratio < MIN_GROUNDED_RATIO:
        return _unmet(
            req, f"only {ratio:.0%} of {f.name}'s distinctive words occur in what was read "
            f"(needs {MIN_GROUNDED_RATIO:.0%})",
        )  # fmt: skip
    return _met(req, f"{ratio:.0%} of {f.name}'s distinctive words occur in what was read")


def as_verifications(task_id: str, results: list[RequirementResult]) -> list[VerificationResult]:
    """Requirement outcomes in the shape the rest of the system stores and displays."""
    return [
        VerificationResult(
            task_id=task_id,
            kind=f"goal:{r.kind}",
            passed=r.status is RequirementStatus.MET,
            description=r.text,
            message=r.evidence[:300],
            evidence={"requirement": r.id, "status": r.status.value},
        )  # fmt: skip
        for r in results
    ]
