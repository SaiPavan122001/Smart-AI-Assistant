"""Tool abstraction: every operation on the computer is an explicit, typed Tool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, Field

from app.models.enums import ApprovalRequirement, Capability, RiskLevel
from app.models.tool import AuditPolicy, RetryPolicy, RiskAssessment, ToolSpec

if TYPE_CHECKING:
    from app.tools.context import ExecutionContext

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
    output_schema: ClassVar[dict[str, Any]] = {}
    returns: ClassVar[str] = (
        ""  # one-line shape of `data` for plan references, e.g. 'matches[{path,name}], scanned'
    )

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(
            name=cls.name,
            description=cls.description,
            input_schema=cls.Input.model_json_schema(),
            output_schema=cls.output_schema,
            returns=cls.returns,
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

    @abstractmethod
    async def run(self, args: InT, ctx: ExecutionContext) -> ToolOutput: ...


def _brief(args: BaseModel, limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.model_dump(exclude_none=True).items())
    return text if len(text) <= limit else text[: limit - 1] + "…"
