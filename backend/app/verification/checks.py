"""Verification checkers: explicit, deterministic contracts over observable state.

Every checker returns ``CheckOutcome`` and never raises for an *unmet* condition
(that is a normal ``passed=False``). Paths are validated through the same PathPolicy
as the filesystem tools, so verification cannot be used to probe outside allowed roots.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.tools.context import ExecutionContext
from app.tools.desktop.models import WindowSel
from app.utils.errors import AgentError, PathViolation


@dataclass
class CheckOutcome:
    passed: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    # The check failed in a way that waiting or retrying cannot change (invalid contract, nothing
    # to observe). Polling stops immediately and recovery does not re-check.
    terminal: bool = False
    # Fingerprint of the observed state. When it stops changing, further polling is pointless.
    state: str | None = None
    polls: int = 1
    duration_ms: float = 0.0


Checker = Callable[[dict[str, Any], ExecutionContext], Awaitable[CheckOutcome]]
REGISTRY: dict[str, Checker] = {}
PARAMS: dict[str, type[BaseModel]] = {}  # kind -> parameter contract (the source of truth)


_EXPECTED_TEXT = ("contains", "equals", "text", "value_contains")


class Params(BaseModel):
    """Base of every verification parameter contract. Unknown parameters are rejected."""

    model_config = ConfigDict(extra="forbid")
    one_of: ClassVar[tuple[str, ...]] = ()  # at least one of these must be given
    note: ClassVar[str] = ""  # usage hint shown to the planner

    @model_validator(mode="after")
    def _need_one(self) -> Params:
        names = type(self).one_of
        if names and all(getattr(self, n) in (None, "") for n in names):
            raise ValueError(f"provide one of: {', '.join(names)}")
        for name in _EXPECTED_TEXT:
            value = getattr(self, name, None)
            if isinstance(value, str) and value and not value.strip():
                # e.g. contains=" ": a placeholder for "something", which proves nothing
                raise ValueError(f"{name}: expected text must contain visible characters")
        return self


def checker(kind: str, params: type[BaseModel]) -> Callable[[Checker], Checker]:
    def deco(fn: Checker) -> Checker:
        REGISTRY[kind] = fn
        PARAMS[kind] = params
        return fn

    return deco


def validate_params(
    kind: str, params: dict[str, Any], *, skip: set[str] | None = None
) -> list[str]:
    """Problems with ``params`` for ``kind`` (empty = valid). ``skip`` = keys filled at run time."""
    model = PARAMS.get(kind)
    if model is None:
        return [f"unknown verification kind '{kind}' (known: {', '.join(sorted(REGISTRY))})"]
    skip = skip or set()
    concrete = {k: v for k, v in params.items() if k not in skip and k != "wait_s"}
    out: list[str] = []
    wait = params.get("wait_s", 0)
    if "wait_s" not in skip and not (
        isinstance(wait, int | float) and not isinstance(wait, bool) and 0 <= wait <= 30
    ):
        out.append("wait_s: must be a number between 0 and 30")
    try:
        model.model_validate(concrete)
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(part) for part in e["loc"])
            if skip and (not loc or (e["type"] == "missing" and loc in skip)):
                continue  # satisfied at run time by a reference
            out.append(f"{loc or 'params'}: {e['msg']}")
    return out


class PathParams(Params):
    path: str = Field(min_length=1)


class FileExistsParams(PathParams):
    kind: Literal["file", "dir"] | None = None
    min_bytes: int | None = Field(default=None, ge=0)


class FileMovedParams(Params):
    src: str = Field(min_length=1, description="source path that must be gone")
    dst: str = Field(min_length=1, description="destination that must exist")


class FileContainsParams(PathParams):
    text: str = Field(min_length=1)
    ignore_case: bool = True


class FilesExistParams(Params):
    base: str = Field(min_length=1)
    files: list[str] = Field(min_length=1, description="paths relative to base")


class DownloadCompleteParams(PathParams):
    min_bytes: int = Field(default=1, ge=0)
    max_bytes: int | None = Field(default=None, ge=0)


class BrowserUrlParams(Params):
    one_of = ("contains", "equals", "regex")
    contains: str | None = None
    equals: str | None = None
    regex: str | None = None


class BrowserTitleParams(Params):
    one_of = ("contains", "equals")
    contains: str | None = None
    equals: str | None = None


class BrowserTextParams(Params):
    note = "only for text you know is on the page; never a guessed or placeholder value"

    contains: str = Field(min_length=1, description="text that must be visible on the page")
    selector: str | None = Field(default=None, description="CSS selector of a VISIBLE element")


class BrowserElementParams(Params):
    query: str = Field(min_length=1)
    role: str | None = None
    present: bool = True


class WindowExistsParams(Params):
    note = (
        "use title_contains; add process only if you know the executable name "
        "(many apps run under a host process whose name differs from the app's)"
    )
    one_of = ("title_contains", "title", "process")
    title_contains: str | None = None
    title: str | None = None
    process: str | None = None
    present: bool = True


class ElementExpect(Params):
    exists: bool | None = None
    enabled: bool | None = None
    value_contains: str | None = None


class ElementStateParams(Params):
    one_of = ("query", "automation_id")
    window_title: str | None = None
    window_process: str | None = None
    query: str | None = None
    role: str | None = None
    automation_id: str | None = None
    expect: ElementExpect = Field(default_factory=ElementExpect)


ValueOp = Literal["equals", "contains", "not_contains", "regex", "gt", "gte", "lt"]
NUMERIC_OPS = frozenset({"gt", "gte", "lt"})


class ValueCheckParams(Params):
    actual: Any = Field(description="usually a reference to a recorded output field")
    op: ValueOp = "equals"
    expected: Any

    @model_validator(mode="after")
    def _comparable(self) -> ValueCheckParams:
        if self.op in NUMERIC_OPS:
            for name in ("actual", "expected"):
                try:
                    float(getattr(self, name))
                except (TypeError, ValueError):
                    raise ValueError(f"{name}: op '{self.op}' needs a number") from None
        if self.op == "regex":
            try:
                re.compile(str(self.expected))
            except re.error as exc:
                raise ValueError(f"expected: invalid regular expression ({exc})") from None
        return self


class _Terminal(AgentError):
    """Nothing to observe: waiting cannot make this check pass."""


def _path(ctx: ExecutionContext, raw: Any) -> Any:
    if not isinstance(raw, str):
        raise PathViolation("path must be a string")
    return ctx.paths.resolve(raw, cwd=ctx.cwd).path


def _need(params: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if params.get(k) in (None, "")]
    if missing:
        raise AgentError(f"verification parameter(s) missing: {', '.join(missing)}")


# ----------------------------------------------------------------------------- filesystem
@checker("file_exists", FileExistsParams)
async def file_exists(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "path")
    path = _path(ctx, p["path"])
    if not path.exists():
        return CheckOutcome(False, f"{path} does not exist", {"path": str(path)})
    kind = p.get("kind")
    if kind == "file" and not path.is_file():
        return CheckOutcome(False, f"{path} exists but is not a file", {"path": str(path)})
    if kind == "dir" and not path.is_dir():
        return CheckOutcome(False, f"{path} exists but is not a folder", {"path": str(path)})
    size = path.stat().st_size if path.is_file() else None
    min_bytes = p.get("min_bytes")
    if min_bytes is not None and (size or 0) < int(min_bytes):
        return CheckOutcome(
            False,
            f"{path.name} is {size} bytes (expected at least {min_bytes})",
            {"path": str(path), "size": size},
        )
    return CheckOutcome(
        True,
        f"{path} exists" + (f" ({size} bytes)" if size is not None else ""),
        {"path": str(path), "size": size},
    )


@checker("dir_exists", PathParams)
async def dir_exists(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    return await file_exists({**p, "kind": "dir"}, ctx)


@checker("file_absent", PathParams)
async def file_absent(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "path")
    path = _path(ctx, p["path"])
    return CheckOutcome(
        not path.exists(),
        f"{path} {'is absent' if not path.exists() else 'still exists'}",
        {"path": str(path)},
    )


@checker("file_moved", FileMovedParams)
async def file_moved(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "src", "dst")
    src, dst = _path(ctx, p["src"]), _path(ctx, p["dst"])
    if dst.is_dir() and src.name != dst.name:
        dst = dst / src.name
    ok = (not src.exists()) and dst.exists()
    return CheckOutcome(
        ok,
        f"source {'absent' if not src.exists() else 'STILL PRESENT'}, destination {'present' if dst.exists() else 'MISSING'}",
        {"src": str(src), "dst": str(dst)},
    )


@checker("file_contains", FileContainsParams)
async def file_contains(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "path", "text")
    path = _path(ctx, p["path"])
    if not path.is_file():
        return CheckOutcome(False, f"{path} is not a file", {"path": str(path)})
    limit = ctx.settings.filesystem.max_read_bytes
    content = path.read_bytes()[:limit].decode("utf-8", errors="replace")
    needle = str(p["text"])
    found = needle.lower() in content.lower() if p.get("ignore_case", True) else needle in content
    return CheckOutcome(
        found,
        f"{path.name} {'contains' if found else 'does not contain'} the expected text",
        {"path": str(path)},
    )


@checker("files_exist", FilesExistParams)
async def files_exist(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "base", "files")
    base = _path(ctx, p["base"])
    missing = [f for f in p["files"] if not (base / f).resolve().exists()]
    # re-validate each joined path through the policy (blocks '..' inside names)
    for f in p["files"]:
        ctx.paths.resolve(str(base / f), cwd=ctx.cwd)
    return CheckOutcome(
        not missing,
        "all files present" if not missing else f"missing: {', '.join(missing)}",
        {"base": str(base), "missing": missing},
    )


@checker("download_complete", DownloadCompleteParams)
async def download_complete(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "path")
    path = _path(ctx, p["path"])
    if not path.is_file():
        return CheckOutcome(False, f"{path.name} not found", {"path": str(path)})
    if path.suffix.lower() in {".crdownload", ".part", ".tmp", ".download"}:
        return CheckOutcome(
            False, "download still in progress (temporary file)", {"path": str(path)}
        )
    s1 = path.stat().st_size
    await asyncio.sleep(0.2)
    s2 = path.stat().st_size
    lo, hi = int(p.get("min_bytes", 1)), p.get("max_bytes")
    ok = s1 == s2 and s2 >= lo and (hi is None or s2 <= int(hi))
    return CheckOutcome(
        ok,
        f"{path.name}: {s2} bytes"
        + ("" if s1 == s2 else " (still growing)")
        + ("" if s2 >= lo else f" < minimum {lo}"),
        {"path": str(path), "size": s2},
    )


# ----------------------------------------------------------------------------- browser
def _browser(ctx: ExecutionContext) -> Any:
    mgr = ctx.services.browser
    sess = mgr.peek(ctx.task_id) if mgr else None
    if sess is None or not sess.has_page:
        raise _Terminal("no open browser page to verify")
    return sess


@checker("browser_url", BrowserUrlParams)
async def browser_url(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    sess = _browser(ctx)
    await sess.refresh_state()
    url = sess.state.current_url
    if "equals" in p:
        ok = url == p["equals"]
    elif "regex" in p:
        ok = re.search(p["regex"], url) is not None
    else:
        _need(p, "contains")
        ok = str(p["contains"]).lower() in url.lower()
    return CheckOutcome(ok, f"current URL is {url}", {"url": url}, state=f"url:{url}")


@checker("browser_title", BrowserTitleParams)
async def browser_title(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    sess = _browser(ctx)
    await sess.refresh_state()
    title = sess.state.page_title
    ok = (
        title == p["equals"]
        if "equals" in p
        else str(p.get("contains", "")).lower() in title.lower()
    )
    return CheckOutcome(
        ok,
        f"page title is '{title}'",
        {"title": title},
        state=f"title:{sess.state.current_url}:{title}",
    )


@checker("browser_text", BrowserTextParams)
async def browser_text(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "contains")
    sess = _browser(ctx)
    data = await sess.read(mode="text", selector=p.get("selector"), max_chars=60000)
    text = data.get("text", "")
    if not p.get(
        "selector"
    ):  # page-wide: the title is part of "what the page says" (it is often absent from body text)
        text = f"{data.get('title', '')} {text}"
    ok = str(p["contains"]).lower() in text.lower()
    return CheckOutcome(
        ok,
        f"page text {'contains' if ok else 'does not contain'} '{str(p['contains'])[:60]}'",
        {"url": data.get("url")},
        state=f"text:{data.get('url')}:{hash(text)}",
    )


@checker("browser_element", BrowserElementParams)
async def browser_element(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "query")
    sess = _browser(ctx)
    res = await sess.find(str(p["query"]), p.get("role"), 3)
    present = bool(res["matches"]) and res["matches"][0]["score"] >= 50
    want = bool(p.get("present", True))
    return CheckOutcome(
        present == want,
        f"element '{p['query']}' is {'present' if present else 'absent'}",
        {"matches": res["matches"][:2]},
        state=f"el:{sess.state.current_url}:{res.get('total_interactive')}:{present}",
    )


# ----------------------------------------------------------------------------- desktop
def _desktop(ctx: ExecutionContext) -> Any:
    svc = ctx.services.desktop
    if svc is None or not svc.available():
        raise _Terminal("desktop verification unavailable")
    return svc


@checker("window_exists", WindowExistsParams)
async def window_exists(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    svc = _desktop(ctx)
    title, proc = (
        (p.get("title_contains") or p.get("title") or "").lower(),
        (p.get("process") or "").lower(),
    )
    wins = await svc.windows()
    hit = [
        w
        for w in wins
        if (not title or title in w.title.lower())
        and (not proc or w.process.lower() in (proc, proc + ".exe"))
    ]
    want = bool(p.get("present", True))
    return CheckOutcome(
        bool(hit) == want,
        f"window matching {title or proc!r} is {'open' if hit else 'not open'}",
        {"windows": [w.title for w in hit[:3]]},
    )


@checker("element_state", ElementStateParams)
async def element_state(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    svc = _desktop(ctx)
    sel = WindowSel(title=p.get("window_title"), process=p.get("window_process"))
    try:
        _snap, ranked, _amb = await svc.find(
            sel,
            query=p.get("query"),
            role=p.get("role"),
            automation_id=p.get("automation_id"),
            limit=1,
        )
    except AgentError as exc:
        return CheckOutcome(False, exc.message)
    exists = bool(ranked)
    expect = p.get("expect", {})
    if "exists" in expect and exists != bool(expect["exists"]):
        return CheckOutcome(
            False,
            f"element {'exists' if exists else 'missing'} (expected exists={expect['exists']})",
        )
    if not exists:
        return CheckOutcome(expect.get("exists", True) is not True, "element not found")
    el = ranked[0][1]
    state = await svc.read_element(el)
    if "enabled" in expect and bool(state.get("enabled", el.enabled)) != bool(expect["enabled"]):
        return CheckOutcome(
            False, f"enabled={state.get('enabled')} (expected {expect['enabled']})", state
        )
    if (
        "value_contains" in expect
        and str(expect["value_contains"]).lower()
        not in str(state.get("value") or state.get("text") or "").lower()
    ):
        return CheckOutcome(
            False, "value does not contain expected text", {"value": str(state.get("value"))[:100]}
        )
    return CheckOutcome(True, f"{el.brief()} matches expectation", {"element": el.brief()})


# ----------------------------------------------------------------------------- generic
@checker("value_check", ValueCheckParams)
async def value_check(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    """Compare a (template-resolved) value from step output. ops: equals, contains, regex, gt, gte, lt, not_contains."""
    op = p.get("op", "equals")
    actual, expected = p.get("actual"), p.get("expected")
    try:
        if op == "equals":
            ok = str(actual) == str(expected)
        elif op == "contains":
            ok = str(expected).lower() in str(actual).lower()
        elif op == "not_contains":
            ok = str(expected).lower() not in str(actual).lower()
        elif op == "regex":
            ok = re.search(str(expected), str(actual)) is not None
        elif op in {"gt", "gte", "lt"}:
            a, e = float(actual or 0), float(expected or 0)
            ok = a > e if op == "gt" else a >= e if op == "gte" else a < e
        else:
            return CheckOutcome(False, f"unknown op '{op}'", terminal=True)
    except (TypeError, ValueError, re.error):
        return CheckOutcome(False, "value_check could not compare the given values", terminal=True)
    return CheckOutcome(
        ok, f"value {op} check {'passed' if ok else 'failed'}", {"actual": str(actual)[:200]}
    )


# Recorded values and resolved references never change by waiting: checked exactly once.
NO_POLL = frozenset({"value_check"})
# Longest a check may poll. Only downloads legitimately need the full 30 s.
POLL_CAP_S = {"download_complete": 30.0}
STABLE_EXIT_S = 3.0  # observed state unchanged this long -> the page is settled; stop polling
_FIRST_DELAY_S, _MAX_DELAY_S = 0.15, 1.0


def poll_cap(kind: str, ctx: ExecutionContext) -> float:
    return POLL_CAP_S.get(kind, float(ctx.settings.limits.verification_max_wait_s))


async def run_checker(
    kind: str, params: dict[str, Any], ctx: ExecutionContext, *, wait_s: float = 0.0
) -> CheckOutcome:
    """Immediate first check, then short bounded polling with backoff.

    Exits early on success, on a terminal failure, and once the observed state stopped changing.
    """
    started = time.monotonic()
    fn = REGISTRY.get(kind)
    if fn is None:
        return CheckOutcome(
            False,
            f"unknown verification kind '{kind}' (known: {', '.join(sorted(REGISTRY))})",
            terminal=True,
        )
    params = {k: v for k, v in params.items() if v is not None}
    problems = validate_params(kind, params)
    if problems:
        return CheckOutcome(
            False, f"invalid {kind} verification: {'; '.join(problems[:4])}", terminal=True
        )
    budget = 0.0
    if wait_s > 0 and kind not in NO_POLL:
        budget = min(wait_s, poll_cap(kind, ctx))
    deadline = started + budget
    delay, polls = _FIRST_DELAY_S, 0
    last_state: str | None = None
    stable_since = started
    while True:
        try:
            outcome = await fn(params, ctx)
        except PathViolation as exc:
            outcome = CheckOutcome(False, f"cannot verify: {exc.message}", terminal=True)
        except _Terminal as exc:
            outcome = CheckOutcome(False, exc.message, terminal=True)
        except AgentError as exc:
            outcome = CheckOutcome(False, exc.message)
        except Exception as exc:
            outcome = CheckOutcome(False, f"verification error: {type(exc).__name__}")
        polls += 1
        now = time.monotonic()
        if outcome.passed or outcome.terminal or now >= deadline:
            break
        if outcome.state is not None:
            if outcome.state != last_state:
                last_state, stable_since = outcome.state, now
            elif now - stable_since >= STABLE_EXIT_S:
                break  # nothing is changing any more: the result is known
        ctx.token.raise_if_cancelled()
        await asyncio.sleep(min(delay, max(0.0, deadline - now)))
        delay = min(delay * 2, _MAX_DELAY_S)
    outcome.polls = polls
    outcome.duration_ms = (time.monotonic() - started) * 1000
    return outcome
