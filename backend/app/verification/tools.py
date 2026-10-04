"""LLM-visible verification tools (read-only wrappers over the verification checkers)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Capability
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.schema import OutputModel
from app.verification.checks import REGISTRY, run_checker


class CheckLine(OutputModel):
    passed: bool
    message: str
    kind: str | None = None


class VerifyOutput(OutputModel):
    passed: bool
    message: str | None = Field(default=None, description="verify_file_state only")
    kind: str | None = Field(default=None, description="verify_file_state only")
    evidence: dict[str, Any] | None = Field(default=None, description="verify_file_state only")
    checks: list[CheckLine] | None = Field(default=None, description="one entry per condition")


class VerifyArtifacts(OutputModel):
    verified: bool


async def _run(kind: str, params: dict[str, Any], ctx: ExecutionContext) -> ToolOutput:
    wait = float(params.pop("wait_s", 0) or 0)
    out = await run_checker(kind, params, ctx, wait_s=wait)
    return ToolOutput(
        data={"passed": out.passed, "message": out.message, "evidence": out.evidence, "kind": kind},
        summary=f"{'PASS' if out.passed else 'FAIL'} [{kind}]: {out.message}",
        artifacts={"verified": out.passed},
    )


class FileStateInput(BaseModel):
    path: str
    expect: str = Field(default="exists", pattern="^(exists|absent|contains|dir)$")
    text: str | None = Field(
        default=None, description="expect=contains: text that must be in the file"
    )
    min_bytes: int | None = None
    wait_s: float = Field(default=0, ge=0, le=30)


class VerifyFileState(Tool[FileStateInput]):
    name = "verify_file_state"
    Output = VerifyOutput
    Artifacts = VerifyArtifacts
    description = "Verify a file/folder state: exists, absent, is a directory, or contains text."
    capability = Capability.VERIFICATION
    Input = FileStateInput
    read_only = True
    idempotent = True
    returns_external_content = True

    async def run(self, args: FileStateInput, ctx: ExecutionContext) -> ToolOutput:
        kind = {
            "exists": "file_exists",
            "absent": "file_absent",
            "contains": "file_contains",
            "dir": "dir_exists",
        }[args.expect]
        params: dict[str, Any] = {"path": args.path, "wait_s": args.wait_s}
        if args.expect == "contains":
            params["text"] = args.text or ""
        if args.min_bytes is not None and args.expect == "exists":
            params["min_bytes"] = args.min_bytes
        return await _run(kind, params, ctx)


class BrowserStateInput(BaseModel):
    url_contains: str | None = None
    title_contains: str | None = None
    text_contains: str | None = None
    element: str | None = Field(
        default=None, description="Element description that must be present"
    )
    wait_s: float = Field(default=0, ge=0, le=30)


class VerifyBrowserState(Tool[BrowserStateInput]):
    name = "verify_browser_state"
    Output = VerifyOutput
    Artifacts = VerifyArtifacts
    description = "Verify the browser state: URL/title/visible text/element presence (all given conditions must hold)."
    capability = Capability.VERIFICATION
    Input = BrowserStateInput
    read_only = True
    returns_external_content = True

    async def run(self, args: BrowserStateInput, ctx: ExecutionContext) -> ToolOutput:
        checks: list[tuple[str, dict[str, Any]]] = []
        if args.url_contains:
            checks.append(("browser_url", {"contains": args.url_contains}))
        if args.title_contains:
            checks.append(("browser_title", {"contains": args.title_contains}))
        if args.text_contains:
            checks.append(("browser_text", {"contains": args.text_contains}))
        if args.element:
            checks.append(("browser_element", {"query": args.element}))
        if not checks:
            checks.append(("browser_url", {"contains": ""}))
        outs = [await run_checker(k, p, ctx, wait_s=args.wait_s) for k, p in checks]
        ok = all(o.passed for o in outs)
        return ToolOutput(
            data={"passed": ok, "checks": [{"kind": k, "passed": o.passed, "message": o.message} for (k, _), o in zip(checks, outs, strict=True)]},
            summary=f"{'PASS' if ok else 'FAIL'}: " + "; ".join(o.message for o in outs), artifacts={"verified": ok},
        )  # fmt: skip


class DesktopStateInput(BaseModel):
    window_title: str | None = None
    window_process: str | None = None
    window_open: bool | None = Field(
        default=None, description="Assert that a matching window is open (true) or closed (false)"
    )
    element_query: str | None = None
    element_role: str | None = None
    automation_id: str | None = None
    expect_enabled: bool | None = None
    expect_value_contains: str | None = None


class VerifyDesktopState(Tool[DesktopStateInput]):
    name = "verify_desktop_state"
    Output = VerifyOutput
    Artifacts = VerifyArtifacts
    description = "Verify desktop state via UI Automation: window open/closed, control exists, enabled, or contains a value."
    capability = Capability.VERIFICATION
    Input = DesktopStateInput
    read_only = True
    returns_external_content = True

    async def run(self, args: DesktopStateInput, ctx: ExecutionContext) -> ToolOutput:
        outs = []
        if args.window_open is not None:
            outs.append(
                await run_checker(
                    "window_exists",
                    {
                        "title_contains": args.window_title,
                        "process": args.window_process,
                        "present": args.window_open,
                    },
                    ctx,
                )
            )
        if args.element_query or args.automation_id:
            expect: dict[str, Any] = {"exists": True}
            if args.expect_enabled is not None:
                expect["enabled"] = args.expect_enabled
            if args.expect_value_contains:
                expect["value_contains"] = args.expect_value_contains
            outs.append(await run_checker("element_state", {"window_title": args.window_title, "window_process": args.window_process,
                                                           "query": args.element_query, "role": args.element_role,
                                                           "automation_id": args.automation_id, "expect": expect}, ctx))  # fmt: skip
        ok = bool(outs) and all(o.passed for o in outs)
        return ToolOutput(data={"passed": ok, "checks": [{"passed": o.passed, "message": o.message} for o in outs]},
                          summary=f"{'PASS' if ok else 'FAIL'}: " + "; ".join(o.message for o in outs), artifacts={"verified": ok})  # fmt: skip


class TaskResultInput(BaseModel):
    criteria: list[dict[str, Any]] = Field(
        description="List of {kind, params} verification specs, e.g. {kind:'file_exists', params:{path:'...'}}"
    )


class VerifyTaskResult(Tool[TaskResultInput]):
    name = "verify_task_result"
    Output = VerifyOutput
    Artifacts = VerifyArtifacts
    description = f"Verify several conditions at once. Kinds: {', '.join(sorted(REGISTRY))}."
    capability = Capability.VERIFICATION
    Input = TaskResultInput
    read_only = True
    returns_external_content = True

    async def run(self, args: TaskResultInput, ctx: ExecutionContext) -> ToolOutput:
        outs = []
        for spec in args.criteria[:20]:
            params = dict(spec.get("params", {}))
            wait = float(params.pop("wait_s", 0) or 0)
            outs.append(
                (
                    spec.get("kind", "?"),
                    await run_checker(str(spec.get("kind")), params, ctx, wait_s=wait),
                )
            )
        ok = bool(outs) and all(o.passed for _, o in outs)
        return ToolOutput(
            data={"passed": ok, "checks": [{"kind": k, "passed": o.passed, "message": o.message} for k, o in outs]},
            summary=f"{'ALL PASS' if ok else 'FAILED'}: {sum(o.passed for _, o in outs)}/{len(outs)} conditions hold", artifacts={"verified": ok},
        )  # fmt: skip


VERIFICATION_TOOLS: list[type[Tool[Any]]] = [
    VerifyFileState,
    VerifyBrowserState,
    VerifyDesktopState,
    VerifyTaskResult,
]
