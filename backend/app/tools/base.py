"""Tool abstraction: every operation on the computer is an explicit, typed Tool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, Field

from app.models.enums import ApprovalRequirement, Capability, RiskLevel
from app.models.tool import AuditPolicy, RetryPolicy, RiskAssessment, ToolSpec
from app.tools.schema import describe

if TYPE_CHECKING:
    from app.tools.context import ExecutionContext, ToolServices

InT = TypeVar("InT", bound=BaseModel)


class ToolOutput(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    perception_source: str | None = None
    untrusted: bool | None = None  # None -> use spec.returns_external_content


class Tool(ABC, Generic[InT]):
    name: ClassVar[str]
    description: ClassVar[str]
    capability: ClassVar[Capability]
    Input: ClassVar[type[BaseModel]]
    risk: ClassVar[RiskLevel] = RiskLevel.LOW
    approval: ClassVar[ApprovalRequirement] = ApprovalRequirement.POLICY
    timeout_s: ClassVar[float] = 30.0
    retry: ClassVar[RetryPolicy] = RetryPolicy()
    idempotent: ClassVar[bool] = False
    read_only: ClassVar[bool] = False
    cacheable: ClassVar[bool] = False
    establishes_state: ClassVar[bool] = False
    cache_ttl_s: ClassVar[float] = 5.0
    audit: ClassVar[AuditPolicy] = AuditPolicy()
    exposed_to_llm: ClassVar[bool] = True
    returns_external_content: ClassVar[bool] = False
    # The output contract. ``Output`` describes ``ToolOutput.data`` and ``Artifacts`` describes
    # ``ToolOutput.artifacts``. They are the single source of truth: the planner's catalogue,
    # plan validation of ``{{steps.sN.data.x}}`` references and the router's contract check are
    # all derived from them. A tool without ``Output`` produces nothing a plan may reference.
    Output: ClassVar[type[BaseModel] | None] = None
    Artifacts: ClassVar[type[BaseModel] | None] = None

    @classmethod
    def output_schema(cls) -> dict[str, Any]:
        return cls.Output.model_json_schema() if cls.Output is not None else {}

    @classmethod
    def artifacts_schema(cls) -> dict[str, Any]:
        return cls.Artifacts.model_json_schema() if cls.Artifacts is not None else {}

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(
            name=cls.name,
            description=cls.description,
            input_schema=cls.Input.model_json_schema(),
            output_schema=cls.output_schema(),
            artifacts_schema=cls.artifacts_schema(),
            returns=describe(cls.output_schema()),
            capability=cls.capability,
            risk=cls.risk,
            approval=cls.approval,
            timeout_s=cls.timeout_s,
            retry=cls.retry,
            idempotent=cls.idempotent,
            audit=cls.audit,
            exposed_to_llm=cls.exposed_to_llm,
            returns_external_content=cls.returns_external_content,
            read_only=cls.read_only,
            cacheable=cls.cacheable,
            establishes_state=cls.establishes_state,
            cache_ttl_s=cls.cache_ttl_s,
        )

    def assess(
        self, args: InT, ctx: ExecutionContext
    ) -> RiskAssessment | Awaitable[RiskAssessment]:
        """Describe *this* call: its real risk, affected resource and a readable action.

        Implementations must be side-effect free (and may be ``async``). They may raise ``PolicyViolation``;
        the router turns that into a hard deny.
        """
        return RiskAssessment(
            risk=self.risk,
            action=f"{self.name}({_brief(args)})",
            resource=_brief(args),
            category=self.capability.value,
        )

    def reconcile(self, args: InT, services: ToolServices) -> Reconciliation:
        """Did a call that was in flight when the process stopped actually happen?

        Read-only and side-effect free: it looks at the observable state only. The default is
        "unknown", which makes the action ``UNCERTAIN`` after a restart: it is never repeated without
        the user's approval. Override it only where the state gives a reliable answer.
        """
        return Reconciliation(None)

    @abstractmethod
    async def run(self, args: InT, ctx: ExecutionContext) -> ToolOutput: ...


@dataclass(frozen=True)
class Reconciliation:
    """``done``: True = it happened, False = it did not, None = cannot be determined."""

    done: bool | None
    detail: str = ""
    output: ToolOutput | None = None  # for done=True: what the call would have returned


def _brief(args: BaseModel, limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.model_dump(exclude_none=True).items())
    return text if len(text) <= limit else text[: limit - 1] + "…"
