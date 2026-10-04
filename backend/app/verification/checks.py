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
from typing import Any

from app.tools.context import ExecutionContext
from app.tools.desktop.models import WindowSel
from app.utils.errors import AgentError, PathViolation


@dataclass
class CheckOutcome:
    passed: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


Checker = Callable[[dict[str, Any], ExecutionContext], Awaitable[CheckOutcome]]
REGISTRY: dict[str, Checker] = {}


def checker(kind: str) -> Callable[[Checker], Checker]:
    def deco(fn: Checker) -> Checker:
        REGISTRY[kind] = fn
        return fn

    return deco


def _path(ctx: ExecutionContext, raw: Any) -> Any:
    if not isinstance(raw, str):
        raise PathViolation("path must be a string")
    return ctx.paths.resolve(raw, cwd=ctx.cwd).path


def _need(params: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if params.get(k) in (None, "")]
    if missing:
        raise AgentError(f"verification parameter(s) missing: {', '.join(missing)}")


# ----------------------------------------------------------------------------- filesystem
@checker("file_exists")
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


@checker("dir_exists")
async def dir_exists(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    return await file_exists({**p, "kind": "dir"}, ctx)


@checker("file_absent")
async def file_absent(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "path")
    path = _path(ctx, p["path"])
    return CheckOutcome(
        not path.exists(),
        f"{path} {'is absent' if not path.exists() else 'still exists'}",
        {"path": str(path)},
    )


@checker("file_moved")
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


@checker("file_contains")
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


@checker("files_exist")
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


@checker("download_complete")
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
        raise AgentError("no open browser page to verify")
    return sess


@checker("browser_url")
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
    return CheckOutcome(ok, f"current URL is {url}", {"url": url})


@checker("browser_title")
async def browser_title(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    sess = _browser(ctx)
    await sess.refresh_state()
    title = sess.state.page_title
    ok = (
        title == p["equals"]
        if "equals" in p
        else str(p.get("contains", "")).lower() in title.lower()
    )
    return CheckOutcome(ok, f"page title is '{title}'", {"title": title})


@checker("browser_text")
async def browser_text(p: dict[str, Any], ctx: ExecutionContext) -> CheckOutcome:
    _need(p, "contains")
    sess = _browser(ctx)
    data = await sess.read(mode="text", selector=p.get("selector"), max_chars=60000)
    text = data.get("text", "")
    ok = str(p["contains"]).lower() in text.lower()
    return CheckOutcome(
        ok,
        f"page text {'contains' if ok else 'does not contain'} '{str(p['contains'])[:60]}'",
        {"url": data.get("url")},
    )


@checker("browser_element")
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
    )


# ----------------------------------------------------------------------------- desktop
def _desktop(ctx: ExecutionContext) -> Any:
    svc = ctx.services.desktop
    if svc is None or not svc.available():
        raise AgentError("desktop verification unavailable")
    return svc


@checker("window_exists")
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


@checker("element_state")
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
@checker("value_check")
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
            return CheckOutcome(False, f"unknown op '{op}'")
    except (TypeError, ValueError):
        return CheckOutcome(False, "value_check could not compare the given values")
    return CheckOutcome(
        ok, f"value {op} check {'passed' if ok else 'failed'}", {"actual": str(actual)[:200]}
    )


async def run_checker(
    kind: str, params: dict[str, Any], ctx: ExecutionContext, *, wait_s: float = 0.0
) -> CheckOutcome:
    fn = REGISTRY.get(kind)
    if fn is None:
        return CheckOutcome(
            False, f"unknown verification kind '{kind}' (known: {', '.join(sorted(REGISTRY))})"
        )
    deadline = time.monotonic() + max(0.0, min(wait_s, 30.0))
    while True:
        try:
            outcome = await fn(params, ctx)
        except PathViolation as exc:
            return CheckOutcome(False, f"cannot verify: {exc.message}")
        except AgentError as exc:
            outcome = CheckOutcome(False, exc.message)
        except Exception as exc:
            outcome = CheckOutcome(False, f"verification error: {type(exc).__name__}")
        if outcome.passed or time.monotonic() >= deadline:
            return outcome
        ctx.token.raise_if_cancelled()
        await asyncio.sleep(0.4)
