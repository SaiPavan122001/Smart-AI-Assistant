"""Planner: LLM -> structured plan -> deterministic validation (-> one repair round)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMRequest, Message
from app.llm.gateway import LLMGateway
from app.models.enums import StepKind
from app.models.plan import Plan, PlanStep
from app.models.verification import VerificationSpec
from app.observability.logging import get_logger
from app.observability.usage import UsageMeter
from app.orchestration.templating import has_template, referenced_steps
from app.planner.prompts import build_planner_system, build_planner_user
from app.tools.context import ToolServices
from app.tools.registry import ToolRegistry
from app.utils.control import CancelToken
from app.utils.errors import AgentError, ErrorCode
from app.verification.checks import REGISTRY as VERIFY_REGISTRY

log = get_logger("planner")
MAX_STEPS = 25


class StepDraft(BaseModel):
    id: str
    title: str
    kind: StepKind = StepKind.TOOL
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    goal: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    rationale: str = ""
    verify: list[VerificationSpec] = Field(default_factory=list)
    optional: bool = False


class PlanDraft(BaseModel):
    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    steps: list[StepDraft] = Field(default_factory=list)
    success_criteria: list[VerificationSpec] = Field(default_factory=list)
    needs_clarification: str | None = None


def validate_plan(
    plan: Plan, registry: ToolRegistry, *, known_prior: set[str] | None = None
) -> list[str]:
    """Return a list of problems (empty = valid). Pure and deterministic."""
    problems: list[str] = []
    if len(plan.steps) > MAX_STEPS:
        problems.append(f"plan has {len(plan.steps)} steps; maximum is {MAX_STEPS}")
    seen: set[str] = set(known_prior or ())
    for step in plan.steps:
        if step.id in seen:
            problems.append(f"duplicate step id '{step.id}'")
        arg_refs = referenced_steps(step.args)
        verify_refs = referenced_steps([v.params for v in step.verify])
        for dep in step.depends_on:
            if dep not in seen:
                problems.append(f"{step.id}: depends_on '{dep}' is not an earlier step")
        for ref in arg_refs:
            if ref not in seen:
                problems.append(
                    f"{step.id}: references {{{{steps.{ref}...}}}} which is not an earlier step"
                )
        for ref in verify_refs:  # a step's own verification may reference its own output
            if ref not in seen and ref != step.id:
                problems.append(
                    f"{step.id}: verification references {{{{steps.{ref}...}}}} which is not an earlier step"
                )
        if step.kind is StepKind.TOOL:
            tool = registry.get(step.tool or "")
            if tool is None or not tool.exposed_to_llm:
                problems.append(f"{step.id}: unknown tool '{step.tool}'")
            else:
                problems += _validate_args(step, tool)
        else:
            for name in step.allowed_tools:
                t = registry.get(name)
                if t is None or not t.exposed_to_llm:
                    problems.append(f"{step.id}: allowed_tools contains unknown tool '{name}'")
        for v in step.verify:
            if v.kind not in VERIFY_REGISTRY:
                problems.append(f"{step.id}: unknown verification kind '{v.kind}'")
        seen.add(step.id)
    for v in plan.success_criteria:
        if v.kind not in VERIFY_REGISTRY:
            problems.append(f"success_criteria: unknown verification kind '{v.kind}'")
        for ref in referenced_steps(v.params):
            if ref not in seen:
                problems.append(f"success_criteria references unknown step '{ref}'")
    return problems


def _validate_args(step: PlanStep, tool: Any) -> list[str]:
    templated = {k for k, v in step.args.items() if has_template(v)}
    concrete = {k: v for k, v in step.args.items() if k not in templated}
    try:
        tool.Input.model_validate(concrete)
    except ValidationError as exc:
        out = []
        for e in exc.errors():
            loc = e["loc"][0] if e["loc"] else None
            if templated and (e["type"] == "missing" or loc is None):
                continue  # satisfied at run time by a reference
            out.append(
                f"{step.id}: {step.tool} args: {'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}"
            )
        return out
    return []


def draft_to_plan(draft: PlanDraft, task_id: str, *, version: int = 1) -> Plan:
    steps = [PlanStep(**s.model_dump()) for s in draft.steps]
    return Plan(
        task_id=task_id, version=version, summary=draft.summary, assumptions=draft.assumptions,
        steps=steps, success_criteria=draft.success_criteria,
    )  # fmt: skip


class PlanOutcome(BaseModel):
    plan: Plan | None = None
    clarification: str | None = None


class Planner:
    def __init__(self, gateway: LLMGateway, registry: ToolRegistry, services: ToolServices) -> None:
        self._gateway = gateway
        self._registry = registry
        self._services = services
        self._temperature = services.settings.llm.temperature

    async def create_plan(
        self, task_id: str, instruction: str, *, meter: UsageMeter, token: CancelToken
    ) -> PlanOutcome:
        user = build_planner_user(instruction, self._registry, self._services)
        return await self._plan(task_id, user, meter=meter, token=token, purpose="plan", version=1)

    async def replan(
        self, task_id: str, instruction: str, plan: Plan, *, failure: str, state_summary: str,
        meter: UsageMeter, token: CancelToken,
    ) -> PlanOutcome:  # fmt: skip
        done = [s for s in plan.steps if s.status.value == "SUCCEEDED"]
        user = (
            build_planner_user(instruction, self._registry, self._services)
            + "\n\n<replan_context>\n"
            "A previous plan was partially executed and hit a problem. Produce a plan for ONLY the remaining work. "
            "Do NOT repeat completed steps; you may reference their outputs as {{steps.<id>.data...}}. "
            "Any action the user denied must not be proposed again.\n"
            f"completed_steps: {json.dumps([{'id': s.id, 'title': s.title, 'summary': s.summary[:200]} for s in done])}\n"
            f"problem: {failure[:1500]}\n"
            f"state: {state_summary[:1500]}\n"
            "Use new step ids that continue after the last existing id.\n</replan_context>"
        )
        return await self._plan(
            task_id, user, meter=meter, token=token, purpose="replan", version=plan.version + 1,
            prior={s.id for s in plan.steps},
        )  # fmt: skip

    async def _plan(
        self, task_id: str, user: str, *, meter: UsageMeter, token: CancelToken, purpose: str, version: int,
        prior: set[str] | None = None,
    ) -> PlanOutcome:  # fmt: skip
        messages = [Message.user(user)]
        system = build_planner_system()
        problems: list[str] = []
        for attempt in range(2):
            req = LLMRequest(
                system=system,
                messages=messages,
                temperature=self._temperature,
                max_tokens=4096,
                purpose=purpose,
            )
            draft = await self._gateway.complete_structured(
                req, PlanDraft, meter=meter, token=token
            )
            if draft.needs_clarification and not draft.steps:
                return PlanOutcome(clarification=draft.needs_clarification)
            try:
                plan = draft_to_plan(draft, task_id, version=version)
                problems = validate_plan(plan, self._registry, known_prior=prior)
            except ValidationError as exc:  # e.g. a tool step without a tool name
                problems = [e["msg"] for e in exc.errors()[:6]]
                plan = None
            if plan is not None and not problems:
                return PlanOutcome(plan=plan)
            log.info(
                "plan rejected by validation", extra={"problems": problems[:6], "attempt": attempt}
            )
            messages = [
                *messages,
                Message.assistant(draft.model_dump_json()),
                Message.user(
                    "The plan failed validation:\n- "
                    + "\n- ".join(problems[:10])
                    + "\nReturn a corrected plan."
                ),
            ]
        raise AgentError(
            "The model could not produce a valid plan: " + "; ".join(problems[:4]),
            code=ErrorCode.PLAN_INVALID, details={"problems": problems[:10]},
        )  # fmt: skip
