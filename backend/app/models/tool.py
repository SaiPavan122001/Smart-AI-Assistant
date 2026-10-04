"""Tool contracts: spec (metadata), call (request) and result (response)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ApprovalRequirement, Capability, RiskLevel
from app.utils.errors import ErrorInfo
from app.utils.ids import new_id


class RetryPolicy(BaseModel):
    max_attempts: int = 1  # total attempts including the first
    backoff_s: float = 0.5
    retry_on: list[str] = Field(default_factory=list)  # ErrorCode values


class AuditPolicy(BaseModel):
    log_arguments: bool = True
    log_result: bool = True
    redact_fields: list[str] = Field(default_factory=list)


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    returns: str = ""  # human/LLM-readable shape of the output `data`
    capability: Capability
    risk: RiskLevel = RiskLevel.LOW  # baseline; assess() may raise it per call
    approval: ApprovalRequirement = ApprovalRequirement.POLICY
    timeout_s: float = 30.0
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotent: bool = False
    audit: AuditPolicy = Field(default_factory=AuditPolicy)
    exposed_to_llm: bool = True
    returns_external_content: bool = False  # output must be treated as untrusted data
    read_only: bool = False  # never mutates the environment
    cacheable: bool = False  # identical call may reuse a fresh cached result (implies read_only)
    establishes_state: bool = (
        False  # open/launch-type: repeating an identical call must not re-run the side effect
    )
    cache_ttl_s: float = 5.0


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tc"))
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    step_id: str | None = None
    # Why the agent wants this; shown verbatim in approval requests.
    rationale: str = ""


class InjectionFinding(BaseModel):
    rule: str
    severity: str  # "low" | "medium" | "high"
    excerpt: str


class ToolResult(BaseModel):
    tool_call_id: str
    tool: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    # Compact text for LLM context; the full ``data`` goes to UI/logs only when needed.
    summary: str = ""
    error: ErrorInfo | None = None
    duration_ms: float = 0.0
    attempts: int = 1
    untrusted: bool = False
    injection_findings: list[InjectionFinding] = Field(default_factory=list)
    cached: bool = False
    # Machine-checkable evidence for the verification engine (paths, urls, ids...).
    artifacts: dict[str, Any] = Field(default_factory=dict)
    perception_source: str | None = None


class RiskAssessment(BaseModel):
    """Per-call risk analysis produced by a tool *before* execution."""

    risk: RiskLevel
    action: str  # human readable: "Delete folder C:\\Users\\me\\Desktop\\Old"
    resource: str  # affected resource
    reasons: list[str] = Field(default_factory=list)
    category: str = "general"  # e.g. file_delete, code_execution, download, install
    hard_deny: str | None = None  # non-None -> never executable, value is the reason
    scoped_grant_allowed: bool = True
    mask_args: list[str] = Field(
        default_factory=list
    )  # argument names never shown/persisted in clear
