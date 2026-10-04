from __future__ import annotations

from collections.abc import Iterable

from app.llm.base import LLMToolDef
from app.models.enums import Capability
from app.models.tool import ToolSpec
from app.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:  # type: ignore[type-arg]
        self._tools: dict[str, Tool] = {}  # type: ignore[type-arg]
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:  # type: ignore[type-arg]
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:  # type: ignore[type-arg]
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, *, capability: Capability | None = None) -> list[ToolSpec]:
        return [
            t.spec()
            for t in self._tools.values()
            if capability is None or t.capability == capability
        ]

    def llm_tools(self, allowed: Iterable[str] | None = None) -> list[LLMToolDef]:
        """Tool definitions shown to the model (only ``exposed_to_llm`` tools)."""
        allow = set(allowed) if allowed else None
        out: list[LLMToolDef] = []
        for t in self._tools.values():
            if not t.exposed_to_llm or (allow is not None and t.name not in allow):
                continue
            schema = t.Input.model_json_schema()
            schema.pop("title", None)
            out.append(LLMToolDef(name=t.name, description=t.description, input_schema=schema))
        return out
