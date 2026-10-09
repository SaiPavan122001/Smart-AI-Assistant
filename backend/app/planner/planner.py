"""Planner: LLM -> structured plan -> deterministic validation (-> ONE repair round)."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMRequest, Message
from app.llm.gateway import LLMGateway
from app.models.enums import StepKind, StepStatus
from app.models.goal import GoalSpec
from app.models.plan import Plan, PlanStep
from app.models.verification import VerificationSpec
from app.observability.logging import get_logger
from app.observability.usage import UsageMeter
from app.planner.prompts import build_planner_system, build_planner_user
from app.planner.references import (
    PlanProblem,
    compatible,
    embeddable,
    expected_type,
    reference_type,
)
from app.tools.context import ToolServices
from app.tools.registry import ToolRegistry
from app.utils.control import CancelToken
from app.utils.errors import AgentError, ErrorCode
from app.utils.templating import (
    full_reference,
    has_template,
    referenced_steps,
    template_refs,
)
from app.verification.checks import NUMERIC_OPS, PARAMS, validate_params

log = get_logger("planner")
MAX_STEPS = 25
MAX_REPAIRS = 1  # one bounded repair round; a second invalid plan is PLAN_INVALID
_OFFICIAL_CLAIM = re.compile(r"\bofficial(ly)?\b", re.IGNORECASE)
_UNAVAILABLE: ContextVar[set[str]] = ContextVar("unavailable_prior_steps", default=set())  # noqa: B039
_REF_TEXT = re.compile(r"\{\{[^}]*\}\}")


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
    answer_template: str | None = None


def validate_plan(
    plan: Plan, registry: ToolRegistry, *, known_prior: Any = None,
    prior_criteria: list[VerificationSpec] | None = None, goal: GoalSpec | None = None,
) -> list[str]:  # fmt: skip
    """Return a list of problems (empty = valid). Pure and deterministic."""
    found = validate_plan_detailed(
        plan, registry, known_prior=known_prior, prior_criteria=prior_criteria, goal=goal
    )
    return [str(p) for p in found]


def validate_plan_detailed(
    plan: Plan, registry: ToolRegistry, *, known_prior: Any = None,
    prior_criteria: list[VerificationSpec] | None = None, goal: GoalSpec | None = None,
) -> list[PlanProblem]:  # fmt: skip
    """Structured validation. ``known_prior`` = steps of an earlier plan version (re-planning):
    their ids are taken and the outputs of the ones that SUCCEEDED may be referenced."""
    problems: list[PlanProblem] = []
    add = problems.append
    if len(plan.steps) > MAX_STEPS:
        add(
            PlanProblem(
                "too_many_steps",
                "plan",
                f"plan has {len(plan.steps)} steps; maximum is {MAX_STEPS}",
            )
        )
    if goal is not None:
        problems += _goal_problems(plan, goal)
    prior = _prior_steps(known_prior)
    # Only prior steps that SUCCEEDED have output. Pending ones are superseded by this plan.
    _UNAVAILABLE.set({sid for sid, st in prior.items() if st.status is not StepStatus.SUCCEEDED})
    steps: dict[str, PlanStep] = dict(prior)
    seen: set[str] = set(prior)
    for step in plan.steps:
        if step.id in seen:
            add(PlanProblem("duplicate_step", step.id, f"duplicate step id '{step.id}'"))
        for dep in step.depends_on:
            if dep not in seen:
                add(
                    PlanProblem(
                        "unknown_step", step.id, f"depends_on '{dep}' is not an earlier step"
                    )
                )
        tool = registry.get(step.tool or "") if step.kind is StepKind.TOOL else None
        if step.kind is StepKind.TOOL:
            if tool is None or not tool.exposed_to_llm:
                add(PlanProblem("unknown_tool", step.id, f"unknown tool '{step.tool}'"))
                tool = None
            else:
                problems += _validate_args(step, tool)
        else:
            for name in step.allowed_tools:
                t = registry.get(name)
                if t is None or not t.exposed_to_llm:
                    add(
                        PlanProblem(
                            "unknown_tool", step.id, f"allowed_tools contains unknown tool '{name}'"
                        )
                    )
        # ---- references in arguments: only earlier steps, only real fields, compatible types
        in_schema = tool.Input.model_json_schema() if tool is not None else {}
        for key, value in step.args.items():
            problems += _check_value(
                value, steps, registry, allowed=seen, where=f"{step.id}.args.{key}",
                expect=expected_type(in_schema, key) if in_schema else "any",
            )  # fmt: skip
        steps[step.id] = step  # a step's own verification may reference its own output
        for i, v in enumerate(step.verify):
            problems += _validate_spec(
                v, steps, registry, allowed=seen | {step.id}, where=f"{step.id}.verify[{i}]"
            )
        seen.add(step.id)
    for i, v in enumerate(plan.success_criteria):
        problems += _validate_spec(v, steps, registry, allowed=seen, where=f"success_criteria[{i}]")
    succeeded = {sid for sid, s in prior.items() if s.status is StepStatus.SUCCEEDED}
    problems += _exclusive_criteria(
        merge_criteria(list(prior_criteria or []), plan.success_criteria, succeeded)
    )
    problems += _validate_answer_template(plan.answer_template, steps, registry, seen)
    return problems


def _goal_problems(plan: Plan, goal: GoalSpec) -> list[PlanProblem]:
    """The plan must respect the user's explicit restrictions (the router enforces them again at run time)."""
    out: list[PlanProblem] = []
    for step in plan.steps:
        names = (
            [step.tool] if step.kind is StepKind.TOOL and step.tool else list(step.allowed_tools)
        )
        for name in names:
            broken = goal.violation(name, step.args if step.kind is StepKind.TOOL else {})
            if broken:
                out.append(
                    PlanProblem(
                        "forbidden_by_request",
                        step.id,
                        f"{name} is forbidden by the request ({broken}); plan the work without it",
                    )  # fmt: skip
                )
    return out


def merge_criteria(
    current: list[VerificationSpec], revised: list[VerificationSpec], succeeded: set[str]
) -> list[VerificationSpec]:
    """A re-plan may ADD success criteria but can never relax the request's contract.

    Criteria that are still checkable (no reference, or references only to steps that succeeded)
    stay in force. Without this a revised plan could swap "the file was renamed" for "some other
    file exists" and the task would end verified without doing what was asked. Criteria that
    depended on a step the revised plan replaces can only be re-stated by it.
    """
    kept = [c for c in current if referenced_steps(c.params) <= succeeded]
    return kept + [c for c in revised if c not in kept]


# One page is current when the task ends, so these can only hold for one value at a time.
_SINGLE_STATE = {"browser_url": "equals", "browser_title": "equals"}


def _exclusive_criteria(criteria: list[VerificationSpec]) -> list[PlanProblem]:
    """success_criteria are ALL checked together, once, against the final state."""
    out: list[PlanProblem] = []
    for kind, key in _SINGLE_STATE.items():
        values = {
            str(c.params[key])
            for c in criteria
            if c.kind == kind and key in c.params and not has_template(c.params[key])
        }
        if len(values) > 1:
            out.append(
                PlanProblem(
                    "invalid_verification",
                    "success_criteria",
                    f"{len(values)} different {kind} '{key}' values can never hold together: "
                    "success_criteria are all checked once, at the end, against the FINAL state "
                    "(criteria already in force stay in force). Check an intermediate page in "
                    "the `verify` of the step that opens it",
                )  # fmt: skip
            )
    return out


def _prior_steps(known_prior: Any) -> dict[str, PlanStep]:
    if not known_prior:
        return {}
    if isinstance(known_prior, dict):
        return dict(known_prior)
    out: dict[str, PlanStep] = {}
    for item in known_prior:
        if isinstance(item, PlanStep):
            out[item.id] = item
        else:  # a bare id: taken, but nothing is known about its output
            out[str(item)] = PlanStep(
                id=str(item), title="(earlier step)", kind=StepKind.ADAPTIVE, goal="(earlier)",
                status=StepStatus.SKIPPED,
            )  # fmt: skip
    return out


def _check_value(
    value: Any, steps: dict[str, PlanStep], registry: ToolRegistry, *, allowed: set[str],
    where: str, expect: str = "any",
) -> list[PlanProblem]:  # fmt: skip
    """Every reference inside ``value`` must exist; its type must fit where it is used."""
    out: list[PlanProblem] = []
    whole = full_reference(value)
    for path in dict.fromkeys(template_refs(value)):
        kind, problem = reference_type(
            path, steps, registry, allowed=allowed, where=where, unavailable=_UNAVAILABLE.get()
        )
        if problem is not None:
            out.append(problem)
            continue
        assert kind is not None
        ref = "{{" + path + "}}"
        if whole == path:
            if not compatible(kind, expect):
                out.append(
                    PlanProblem(
                        "type_mismatch",
                        where,
                        f"{ref} is a {kind} but a {expect} is expected here",
                        path,
                    )  # fmt: skip
                )
        elif isinstance(value, str) and not embeddable(kind):
            out.append(
                PlanProblem(
                    "type_mismatch",
                    where,
                    f"{ref} is a {kind} and cannot be placed inside text; reference one of its "
                    "fields (or .length)",
                    path,
                )  # fmt: skip
            )
    return out


def _validate_spec(
    spec: VerificationSpec, steps: dict[str, PlanStep], registry: ToolRegistry, *,
    allowed: set[str], where: str,
) -> list[PlanProblem]:  # fmt: skip
    """A verification contract: known kind, valid parameters, real references, comparable types."""
    model = PARAMS.get(spec.kind)
    if model is None:
        return [
            PlanProblem(
                "invalid_verification",
                where,
                f"unknown verification kind '{spec.kind}'",
                available=sorted(PARAMS),
            )  # fmt: skip
        ]
    out: list[PlanProblem] = []
    schema = model.model_json_schema()
    templated = {k for k, v in spec.params.items() if has_template(v)}
    for key, value in spec.params.items():
        expect = expected_type(schema, key)
        if spec.kind == "value_check" and key == "actual":
            expect = "number" if spec.params.get("op") in NUMERIC_OPS else "any"
        out += _check_value(
            value, steps, registry, allowed=allowed, where=f"{where}.{key}", expect=expect
        )
    for text in validate_params(spec.kind, spec.params, skip=templated):
        out.append(
            PlanProblem(
                "invalid_verification",
                where,
                f"{spec.kind}: {text}. Give a concrete expected value taken from the request or "
                "from a step output; if you cannot state one, REMOVE this check (a step may have "
                "no verify)",
            )  # fmt: skip
        )
    return out


def _validate_answer_template(
    template: str | None, steps: dict[str, PlanStep], registry: ToolRegistry, seen: set[str]
) -> list[PlanProblem]:
    if not template:
        return []
    out: list[PlanProblem] = []
    for path in dict.fromkeys(template_refs(template)):
        parts = path.split(".")
        if len(parts) > 1 and parts[0] == "steps" and parts[1] not in seen:
            out.append(
                PlanProblem(
                    "unknown_step",
                    "answer_template",
                    f"answer_template references unknown step '{parts[1]}'",
                    path,
                )  # fmt: skip
            )
            continue
        kind, problem = reference_type(
            path,
            steps,
            registry,
            allowed=seen,
            where="answer_template",
            unavailable=_UNAVAILABLE.get(),
        )
        if problem is not None:
            out.append(problem)
        elif kind is not None and not embeddable(kind):
            out.append(
                PlanProblem(
                    "type_mismatch",
                    "answer_template",
                    "{{" + path + "}} is a " + kind + " and cannot be shown as text; reference one "
                    "of its fields (or .length)",
                    path,
                )  # fmt: skip
            )
    if _OFFICIAL_CLAIM.search(_REF_TEXT.sub("", template)):
        out.append(
            PlanProblem(
                "unverified_claim",
                "answer_template",
                "the template must not state that a source is 'official' in its own words. "
                "Officialness is established only by the domain check of web_search: use "
                "{{steps.<id>.data.source_note}} to report it",
            )  # fmt: skip
        )
    return out


def _validate_args(step: PlanStep, tool: Any) -> list[PlanProblem]:
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
            where = ".".join(str(p) for p in e["loc"]) or "args"
            out.append(
                PlanProblem("invalid_args", step.id, f"{step.tool} args: {where}: {e['msg']}")
            )
        return out
    return []


def _free_ids(used: set[str], n: int = 6) -> str:
    """'Use s4, s5, ...' — the next unused ids after the highest numeric one."""
    nums = [int(i[1:]) for i in used if i[:1] == "s" and i[1:].isdigit()]
    start = max(nums, default=0) + 1
    return "Use ids " + ", ".join(f"s{start + k}" for k in range(n)) + " ... in order."


def _next_ids(plan: Plan) -> str:
    return _free_ids({s.id for s in plan.steps})


def draft_to_plan(draft: PlanDraft, task_id: str, *, version: int = 1) -> Plan:
    steps = [PlanStep(**s.model_dump()) for s in draft.steps]
    return Plan(
        task_id=task_id, version=version, summary=draft.summary, assumptions=draft.assumptions,
        steps=steps, success_criteria=draft.success_criteria, answer_template=draft.answer_template,
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
        self, task_id: str, instruction: str, *, meter: UsageMeter, token: CancelToken,
        goal: GoalSpec | None = None,
    ) -> PlanOutcome:  # fmt: skip
        user = build_planner_user(instruction, self._registry, self._services, goal)
        return await self._plan(
            task_id, user, meter=meter, token=token, purpose="plan", version=1, goal=goal
        )

    async def replan(
        self, task_id: str, instruction: str, plan: Plan, *, failure: str, state_summary: str,
        meter: UsageMeter, token: CancelToken,
    ) -> PlanOutcome:  # fmt: skip
        done = [s for s in plan.steps if s.status is StepStatus.SUCCEEDED]
        performed = [
            {"id": s.id, "title": s.title, "tool": s.tool, "args": s.args}
            for s in plan.steps
            if s.status is StepStatus.FAILED and (s.output or {}).get("action_performed")
        ]
        goal = plan.goal
        user = (
            build_planner_user(instruction, self._registry, self._services, goal)
            + "\n\n<replan_context>\n"
            "A previous plan was partially executed and hit a problem. Produce a plan for ONLY the remaining work. "
            "Do NOT repeat completed steps; you may reference the outputs of COMPLETED steps as "
            "{{steps.<id>.data.<field>}} (fields of that step's tool only). Steps that failed have no output. "
            "Any action the user denied must not be proposed again. The success_criteria already in force "
            "still apply and cannot be relaxed or replaced: you may only add criteria. If the request cannot "
            "be met as asked, return NO steps and say why in needs_clarification.\n"
            f"completed_steps: {json.dumps([{'id': s.id, 'title': s.title, 'tool': s.tool or 'adaptive', 'summary': s.summary[:200]} for s in done])}\n"
            "performed_but_unverified: these actions DID run and their effects are real; only their verification "
            "failed. Do NOT plan them again (never re-open/re-launch); inspect the current state instead: "
            f"{json.dumps(performed)[:1200]}\n"
            f"success_criteria_in_force: {json.dumps([{'kind': c.kind, 'params': c.params} for c in plan.success_criteria])[:1500]}\n"
            f"problem: {failure[:1500]}\n"
            f"state: {state_summary[:1500]}\n"
            f"existing_step_ids: {sorted(s.id for s in plan.steps)}\n"
            f"Every new step id MUST be unused. {_next_ids(plan)}\n</replan_context>"
        )
        return await self._plan(
            task_id, user, meter=meter, token=token, purpose="replan", version=plan.version + 1,
            prior={s.id: s for s in plan.steps}, prior_criteria=plan.success_criteria, goal=goal,
        )  # fmt: skip

    async def _plan(
        self, task_id: str, user: str, *, meter: UsageMeter, token: CancelToken, purpose: str, version: int,
        prior: dict[str, PlanStep] | None = None,
        prior_criteria: list[VerificationSpec] | None = None, goal: GoalSpec | None = None,
    ) -> PlanOutcome:  # fmt: skip
        """LLM plan -> schema validation -> structured errors -> ONE repair -> validate again.

        A plan that is still invalid after the repair round is PLAN_INVALID: no further model
        call is made, so an unfixable plan can never turn into a planning loop.
        """
        messages = [Message.user(user)]
        system = build_planner_system()
        problems: list[PlanProblem] = []
        for attempt in range(MAX_REPAIRS + 1):
            req = LLMRequest(
                system=system,
                messages=messages,
                temperature=self._temperature,
                max_tokens=4096,
                purpose=purpose if attempt == 0 else "repair",
            )
            draft = await self._gateway.complete_structured(
                req, PlanDraft, meter=meter, token=token
            )
            if draft.needs_clarification and not draft.steps:
                return PlanOutcome(clarification=draft.needs_clarification)
            plan: Plan | None
            try:
                plan = draft_to_plan(draft, task_id, version=version)
                problems = validate_plan_detailed(
                    plan,
                    self._registry,
                    known_prior=prior,
                    prior_criteria=prior_criteria,
                    goal=goal,
                )
            except ValidationError as exc:  # e.g. a tool step without a tool name
                problems = [PlanProblem("invalid_step", "plan", e["msg"]) for e in exc.errors()[:6]]
                plan = None
            if plan is not None and not problems:
                return PlanOutcome(plan=plan)
            log.info(
                "plan rejected by validation",
                extra={"problems": [str(p) for p in problems[:6]], "attempt": attempt},
            )
            if attempt >= MAX_REPAIRS:
                break
            messages = [
                *messages,
                Message.assistant(draft.model_dump_json()),
                Message.user(_repair_message(problems, prior)),
            ]
        raise AgentError(
            "The model could not produce a valid plan: " + "; ".join(str(p) for p in problems[:4]),
            code=ErrorCode.PLAN_INVALID,
            details={"problems": [p.as_dict() for p in problems[:10]]},
        )


def _repair_message(problems: list[PlanProblem], prior: dict[str, PlanStep] | None) -> str:
    text = (
        "The plan failed validation and was NOT executed. Each problem is listed with the exact "
        "location and, for a bad reference, the fields that really exist:\n"
        + json.dumps([p.as_dict() for p in problems[:10]], indent=1)
        + "\nFix exactly these problems. A reference may only use a field listed after '-> data:' "
        "for that step's tool (adaptive steps expose data.answer, data.summary, data.iterations, "
        "data.tool_calls). If no listed field holds what you need, add a step with a tool that "
        "produces it, or drop the reference."
    )
    if prior and any(p.code == "duplicate_step" for p in problems):
        text += (
            "\nThese ids already exist and must not be reused: "
            + ", ".join(sorted(prior))
            + f". {_free_ids(set(prior))}"
        )
    return text + "\nReturn the corrected plan."
