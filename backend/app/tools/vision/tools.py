"""Vision tools: explicit capture + analysis. Never invoked automatically."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Capability, RiskLevel
from app.models.tool import RiskAssessment
from app.tools.base import Tool, ToolOutput
from app.tools.context import ExecutionContext
from app.tools.desktop.models import WindowSel
from app.tools.schema import OutputModel
from app.tools.vision.service import ObservationRequest, VisionService, result_to_data
from app.utils.errors import AgentError, ErrorCode


def _vision(ctx: ExecutionContext) -> VisionService:
    svc = ctx.services.vision
    if svc is None:
        raise AgentError("Vision is not enabled in this deployment", code=ErrorCode.NOT_AVAILABLE)
    return svc


class CaptureOutput(OutputModel):
    capture_id: str
    source: str
    size: list[int] = Field(description="[width, height]")
    window: str | None = None


class VisionElementOut(OutputModel):
    ref: str
    label: str
    kind: str
    confidence: float


class AnalyzeOutput(OutputModel):
    vision_id: str
    confidence: float
    needs_confirmation: bool
    answer: str = Field(description="what the vision model saw / read")
    verdict: bool | None = Field(default=None, description="mode=verify")
    text: str | None = Field(default=None, description="mode=read_text")
    elements: list[VisionElementOut]


class CaptureInput(BaseModel):
    source: str = Field(default="window", pattern="^(window|screen|browser)$")
    window_title: str | None = Field(
        default=None, description="source=window: title substring (default: active window)"
    )
    window_process: str | None = None
    full_page: bool = False
    reason: str = Field(
        description="Why structured perception (DOM/UI Automation) is insufficient", min_length=8
    )


class VisionCapture(Tool[CaptureInput]):
    name = "vision_capture"
    Output = CaptureOutput
    description = (
        "FALLBACK ONLY. Capture a screenshot (window/screen/browser) for vision_analyze. Use only when DOM/accessibility "
        "and UI Automation cannot answer the question; the reason is recorded."
    )
    capability = Capability.VISION
    Input = CaptureInput
    risk = RiskLevel.MEDIUM  # screenshots can contain sensitive on-screen data
    timeout_s = 20.0

    def assess(self, args: CaptureInput, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.MEDIUM, action=f"Capture a screenshot of the {args.source}", resource=args.window_title or args.source,
            reasons=["The screenshot may include sensitive on-screen content and will be analysed by a vision model"],
            category="screen_capture",
        )  # fmt: skip

    async def run(self, args: CaptureInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _vision(ctx)
        if not svc.enabled and svc.ocr is None:
            raise AgentError("No vision/OCR provider configured", code=ErrorCode.NOT_AVAILABLE)
        if args.source == "browser":
            mgr = ctx.services.browser
            sess = mgr.peek(ctx.task_id) if mgr else None
            if sess is None or not sess.has_page:
                raise AgentError("No open browser tab to capture", code=ErrorCode.NOT_FOUND)
            png = await sess.screenshot(full_page=args.full_page)
            meta = svc.store_capture(
                ctx.task_id, png, source="browser", window_title=sess.state.page_title
            )
        else:
            desk = ctx.services.desktop
            if desk is None:
                raise AgentError("Desktop access is not enabled", code=ErrorCode.NOT_AVAILABLE)
            if args.source == "window":
                win = await desk.resolve_window(
                    WindowSel(title=args.window_title, process=args.window_process)
                )
                reason = desk.blocked_reason(win, for_input=False)
                if reason:
                    raise AgentError(reason, code=ErrorCode.POLICY_DENIED)
                r = win.rect
                png = await desk._run(
                    desk.fallback.screenshot_region,
                    r.left,
                    r.top,
                    r.right - r.left,
                    r.bottom - r.top,
                )
                meta = svc.store_capture(
                    ctx.task_id,
                    png,
                    source="window",
                    origin=(r.left, r.top),
                    window_handle=win.handle,
                    window_title=win.title,
                )
            else:
                active = await desk.active()
                if active is not None and (reason := desk.blocked_reason(active, for_input=False)):
                    raise AgentError(
                        f"Refusing full-screen capture while a protected window is active: {reason}",
                        code=ErrorCode.POLICY_DENIED,
                    )
                import ctypes

                u = ctypes.windll.user32  # type: ignore[attr-defined]
                w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
                png = await desk._run(desk.fallback.screenshot_region, 0, 0, w, h)
                meta = svc.store_capture(ctx.task_id, png, source="screen", origin=(0, 0))
        ctx.extras.setdefault("vision_reasons", []).append(args.reason)
        return ToolOutput(
            data={"capture_id": meta.id, "source": meta.source, "size": [meta.width, meta.height], "window": meta.window_title},
            summary=f"Captured {meta.source} ({meta.width}x{meta.height}) as {meta.id}; analyse with vision_analyze",
            perception_source="vision",
        )  # fmt: skip


class AnalyzeInput(BaseModel):
    capture_id: str
    goal: str = Field(description="What to locate / read / verify, e.g. 'the green Run button'")
    mode: str = Field(default="locate", pattern="^(locate|describe|read_text|verify)$")


class VisionAnalyze(Tool[AnalyzeInput]):
    name = "vision_analyze"
    Output = AnalyzeOutput
    description = (
        "FALLBACK ONLY. Analyse a captured screenshot with the vision model. Returns structured elements with confidence. "
        "Low-confidence results must not be acted on; re-capture or ask the user."
    )
    capability = Capability.VISION
    Input = AnalyzeInput
    risk = RiskLevel.MEDIUM
    timeout_s = 90.0
    read_only = True
    returns_external_content = True  # model output derived from screen content

    def assess(self, args: AnalyzeInput, ctx: ExecutionContext) -> RiskAssessment:
        return RiskAssessment(
            risk=RiskLevel.MEDIUM, action=f"Send screenshot {args.capture_id} to the vision model: {args.goal[:80]}",
            resource=args.capture_id, reasons=["The image is sent to the configured vision model provider"],
            category="vision_analysis",
        )  # fmt: skip

    async def run(self, args: AnalyzeInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _vision(ctx)
        res = await svc.analyze(
            ctx.task_id, ObservationRequest(capture_id=args.capture_id, mode=args.mode, goal=args.goal),
            meter=ctx.meter, token=ctx.token,
        )  # fmt: skip
        data = result_to_data(res)
        note = (
            " LOW CONFIDENCE: do not act on this without another observation or user confirmation."
            if res.needs_confirmation
            else ""
        )
        return ToolOutput(
            data=data, summary=f"Vision ({args.mode}) confidence {res.confidence:.2f}: {res.answer[:160]}{note}",
            perception_source="vision",
        )  # fmt: skip


VISION_TOOLS: list[type[Tool[Any]]] = [VisionCapture, VisionAnalyze]
