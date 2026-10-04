"""Provider-agnostic LLM contracts (messages, tool calling, structured output)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    media_type: str = "image/png"
    data_b64: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role="assistant", content=[TextBlock(text=text)])


class LLMToolDef(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class LLMRequest(BaseModel):
    system: str
    messages: list[Message]
    tools: list[LLMToolDef] = Field(default_factory=list)
    # None/"auto": model decides; "any": must call a tool; otherwise: must call that tool
    tool_choice: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    response_schema: dict[str, Any] | None = None  # request JSON matching this schema
    response_schema_name: str = "response"
    purpose: str = "agent"  # for accounting: plan | step | replan | verify | summarize | vision


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class LLMResponse(BaseModel):
    text: str = ""
    tool_uses: list[ToolUseBlock] = Field(default_factory=list)
    structured: dict[str, Any] | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model: str = ""
    stop_reason: str = ""
    latency_ms: float = 0.0


class LLMProvider(ABC):
    """Implementations translate the neutral request/response into a vendor wire format."""

    name: str = "abstract"
    model: str = ""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None
