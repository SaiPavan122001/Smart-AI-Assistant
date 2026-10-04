"""Provider-neutral conformance: every adapter maps the SAME normalized request/response contract."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.config.settings import LLMSettings, Settings, VisionSettings
from app.llm.anthropic import AnthropicProvider
from app.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMToolDef,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.llm.capabilities import ModelCapabilities, require_for_agent, resolve_capabilities
from app.llm.factory import build_llm_provider, build_vision_provider
from app.llm.gemini import GeminiProvider
from app.llm.openai import OpenAIProvider
from app.llm.openrouter import OpenRouterProvider
from app.llm.registry import provider_names
from app.llm.schema import to_gemini_schema
from app.utils.errors import ErrorCode, LLMError

TOOL = LLMToolDef(
    name="fs_list",
    description="list",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


def mock(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- one canned "model called fs_list(path=@Desktop)" response per wire format
def openai_style(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"model": "m", "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "fs_list", "arguments": '{"path": "@Desktop"}'}}]}}], "usage": {"prompt_tokens": 11, "completion_tokens": 4}})  # fmt: skip


def anthropic_style(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"model": "m", "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "tu_1", "name": "fs_list", "input": {"path": "@Desktop"}}],
                                     "usage": {"input_tokens": 11, "output_tokens": 4}})  # fmt: skip


def gemini_style(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"modelVersion": "m", "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
        {"functionCall": {"name": "fs_list", "args": {"path": "@Desktop"}}, "thoughtSignature": "SIG"}]}}], "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4}})  # fmt: skip


ADAPTERS = {
    "openai": (lambda: OpenAIProvider(api_key="k", model="gpt-4.1", client=mock(openai_style))),
    "openrouter": (
        lambda: OpenRouterProvider(api_key="k", model="openai/gpt-4.1", client=mock(openai_style))
    ),
    "openai_compatible": (
        lambda: OpenAIProvider(
            api_key="k",
            model="llama3",
            base_url="http://localhost:11434/v1",
            client=mock(openai_style),
            name="openai_compatible",
        )
    ),
    "anthropic": (
        lambda: AnthropicProvider(api_key="k", model="claude-x", client=mock(anthropic_style))
    ),
    "gemini": (
        lambda: GeminiProvider(api_key="k", model="gemini-2.0-flash", client=mock(gemini_style))
    ),
}


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_every_adapter_returns_the_same_normalized_tool_call(name: str) -> None:
    p: LLMProvider = ADAPTERS[name]()
    r = await p.complete(
        LLMRequest(
            system="s", messages=[Message.user("list my desktop")], tools=[TOOL], tool_choice="any"
        )
    )
    assert len(r.tool_uses) == 1
    tu = r.tool_uses[0]
    assert (tu.name, tu.input) == ("fs_list", {"path": "@Desktop"}) and tu.id
    assert (r.usage.input_tokens, r.usage.output_tokens) == (11, 4) and r.latency_ms >= 0
    assert p.name == name and isinstance(p.capabilities, ModelCapabilities)
    assert not hasattr(r, "choices") and not hasattr(r, "candidates"), (
        "no vendor structures leak into the response"
    )


def test_registry_covers_every_configurable_provider() -> None:
    from typing import get_args

    from app.config.settings import ProviderName

    assert set(provider_names()) == set(get_args(ProviderName)) - {"none"}


# ----------------------------------------------------------------------------- Gemini specifics
async def test_gemini_request_mapping_tool_results_and_thought_signatures() -> None:
    seen: dict[str, Any] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = (
            str(req.url),
            dict(req.headers),
            json.loads(req.content),
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{"text": "done"}, {"text": "hidden", "thought": True}]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 1,
                    "thoughtsTokenCount": 7,
                },
            },
        )

    p = GeminiProvider(api_key="g-key", model="models/gemini-2.0-flash", client=mock(h))
    tu = ToolUseBlock(
        id="call_abc",
        name="fs_list",
        input={"path": "x"},
        provider_state={"thoughtSignature": "SIG123"},
    )
    req = LLMRequest(system="SYS", temperature=0.2, max_tokens=99, tools=[TOOL], tool_choice="fs_list", messages=[
        Message.user("go"), Message(role="assistant", content=[TextBlock(text="calling"), tu]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call_abc", content="[]"), ToolResultBlock(tool_use_id="nope", content="boom", is_error=True)])])  # fmt: skip
    r = await p.complete(req)
    b = seen["body"]
    assert (
        seen["url"].endswith("/v1beta/models/gemini-2.0-flash:generateContent")
        and seen["headers"]["x-goog-api-key"] == "g-key"
    )
    assert b["systemInstruction"]["parts"][0]["text"] == "SYS" and b["generationConfig"] == {
        "maxOutputTokens": 99,
        "temperature": 0.2,
    }
    assert b["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["fs_list"]}
    }
    assert b["tools"][0]["functionDeclarations"][0]["parameters"]["required"] == ["path"]
    assert [c["role"] for c in b["contents"]] == ["user", "model", "user"]
    model_parts = b["contents"][1]["parts"]
    assert model_parts[1] == {
        "functionCall": {"name": "fs_list", "args": {"path": "x"}},
        "thoughtSignature": "SIG123",
    }
    responses = b["contents"][2]["parts"]
    assert responses[0]["functionResponse"] == {"name": "fs_list", "response": {"result": "[]"}}
    assert responses[1]["functionResponse"]["response"] == {"error": "boom"}
    assert r.text == "done" and r.usage.output_tokens == 8, (
        "thought parts are not shown; thought tokens are counted"
    )


async def test_gemini_structured_uses_json_mode_and_blocked_prompts_fail() -> None:
    def ok(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert (
            body["generationConfig"]["responseMimeType"] == "application/json"
            and "tools" not in body
            and "JSON Schema" in body["systemInstruction"]["parts"][0]["text"]
        )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]}
        )

    p = GeminiProvider(api_key="k", model="gemini-2.0-flash", client=mock(ok))
    assert (
        await p.complete(
            LLMRequest(system="s", messages=[Message.user("x")], response_schema={"type": "object"})
        )
    ).structured == {"a": 1}
    blocked = GeminiProvider(
        api_key="k",
        model="gemini-2.0-flash",
        client=mock(
            lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        ),
    )
    with pytest.raises(LLMError, match="SAFETY"):
        await blocked.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    bad = GeminiProvider(
        api_key="k",
        model="gemini-2.0-flash",
        client=mock(
            lambda r: httpx.Response(
                200, json={"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]}
            )
        ),
    )
    with pytest.raises(LLMError) as ei:
        await bad.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    assert ei.value.retryable


def test_gemini_schema_translation() -> None:
    from pydantic import BaseModel

    class Inner(BaseModel):
        k: int | None = None

    class Args(BaseModel):
        path: str
        mode: str = "a"
        items: list[Inner] = []
        extra: dict[str, Any] = {}
        opt: str | None = None

    out = to_gemini_schema(Args.model_json_schema())
    dumped = json.dumps(out)
    for forbidden in ("$ref", "$defs", "anyOf", "title", "default", "additionalProperties"):
        assert forbidden not in dumped, forbidden
    assert out["required"] == ["path"] and out["properties"]["opt"] == {
        "type": "string",
        "nullable": True,
    }
    assert out["properties"]["items"]["items"]["properties"]["k"]["nullable"] is True


# ----------------------------------------------------------------------------- OpenRouter / compatible / OpenAI
async def test_openrouter_headers_base_url_and_error_in_200() -> None:
    seen: dict[str, Any] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"] = str(req.url), dict(req.headers)
        return httpx.Response(
            200, json={"error": {"message": "No endpoints found for x/y", "code": 404}}
        )

    p = OpenRouterProvider(
        api_key="or-key",
        model="anthropic/claude-x",
        app_name="My App",
        app_url="https://me.example",
        client=mock(h),
    )
    with pytest.raises(LLMError, match="No endpoints"):
        await p.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    assert (
        seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
        and seen["headers"]["authorization"] == "Bearer or-key"
    )
    assert (
        seen["headers"]["x-title"] == "My App"
        and seen["headers"]["http-referer"] == "https://me.example"
    )


async def test_openai_reasoning_models_use_max_completion_tokens_and_no_temperature() -> None:
    seen: dict[str, Any] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    for model, expect_temp in (("gpt-5", False), ("gpt-4.1", True)):
        seen.clear()
        await OpenAIProvider(api_key="k", model=model, client=mock(h)).complete(
            LLMRequest(system="s", messages=[Message.user("x")], max_tokens=77)
        )
        assert ("temperature" in seen) is expect_temp
        assert seen.get("max_completion_tokens" if not expect_temp else "max_tokens") == 77


async def test_models_without_native_schema_get_validated_compat_mode() -> None:
    seen: dict[str, Any] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '```json\n{"a": 2}\n```'}}], "usage": {}}
        )

    p = OpenAIProvider(api_key="k", model="llama-3.1-70b", base_url="http://x/v1", client=mock(h))
    assert not p.capabilities.supports_json_schema
    r = await p.complete(
        LLMRequest(
            system="SYS",
            messages=[Message.user("x")],
            response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
    )
    assert (
        "response_format" not in seen
        and "JSON Schema" in seen["messages"][0]["content"]
        and r.structured == {"a": 2}
    )


async def test_malformed_tool_arguments_are_surfaced_not_repaired() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {"name": "fs_list", "arguments": "{not json"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {},
            },
        )

    r = await OpenAIProvider(api_key="k", model="gpt-4.1", client=mock(h)).complete(
        LLMRequest(system="s", messages=[Message.user("x")], tools=[TOOL])
    )
    assert r.tool_uses[0].input == {
        "_raw": "{not json"
    }  # the router's schema validation will reject it


# ----------------------------------------------------------------------------- capabilities
def test_capability_resolution_is_conservative_and_overridable() -> None:
    assert (
        resolve_capabilities("openai/gpt-4.1").supports_vision
        and resolve_capabilities("anthropic/claude-sonnet-4").supports_vision
    )
    assert resolve_capabilities("gpt-5").max_tokens_param == "max_completion_tokens"
    assert not resolve_capabilities("o1-mini").supports_tools
    assert not resolve_capabilities("meta-llama/llama-3.1-8b-instruct:free").supports_json_schema
    unknown = resolve_capabilities("brand-new-model-9000")
    assert unknown.source == "assumed" and not unknown.supports_vision, "vision is never assumed"
    over = resolve_capabilities("llama3", {"supports_tools": False, "supports_vision": True})
    assert over.source == "override" and not over.supports_tools and over.supports_vision
    with pytest.raises(LLMError):
        resolve_capabilities("x", {"made_up": True})


def test_agent_preflight_rejects_models_that_cannot_select_tools() -> None:
    with pytest.raises(LLMError, match="tool/function calling") as ei:
        require_for_agent(resolve_capabilities("o1-mini"), model="o1-mini")
    assert ei.value.code == ErrorCode.NOT_AVAILABLE
    with pytest.raises(LLMError, match="system prompt"):
        require_for_agent(ModelCapabilities(supports_system_prompt=False), model="m")
    warnings = require_for_agent(resolve_capabilities("llama-3.1-8b"), model="llama-3.1-8b")
    assert any("compatibility mode" in w for w in warnings) and any(
        "assumed" in w or "parallel" in w for w in warnings
    )
    assert require_for_agent(resolve_capabilities("gpt-4.1"), model="gpt-4.1") == []


# ----------------------------------------------------------------------------- factory / settings
class FakeSecrets:
    name = "fake"

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.parametrize(
    ("provider", "env", "cls"),
    [
        ("openai", "OPENAI_API_KEY", OpenAIProvider),
        ("anthropic", "ANTHROPIC_API_KEY", AnthropicProvider),
        ("gemini", "GEMINI_API_KEY", GeminiProvider),
        ("gemini", "GOOGLE_API_KEY", GeminiProvider),
        ("openrouter", "OPENROUTER_API_KEY", OpenRouterProvider),
    ],
)
def test_factory_builds_each_provider_from_vendor_env_keys(
    provider: str, env: str, cls: type
) -> None:
    s = Settings(llm=LLMSettings(provider=provider, model="some-model"))  # type: ignore[arg-type]
    assert build_llm_provider(s, FakeSecrets({})) is None, "no key -> unconfigured, not a crash"
    p = build_llm_provider(s, FakeSecrets({env: "k-123456789"}))
    assert isinstance(p, cls) and p.model == "some-model"


def test_openai_compatible_requires_base_url_but_not_key() -> None:
    from app.utils.errors import AgentError

    with pytest.raises(AgentError, match="base URL"):
        build_llm_provider(
            Settings(llm=LLMSettings(provider="openai_compatible", model="m")), FakeSecrets({})
        )
    p = build_llm_provider(
        Settings(
            llm=LLMSettings(
                provider="openai_compatible", model="llama3", base_url="http://localhost:11434/v1"
            )
        ),
        FakeSecrets({}),
    )
    assert isinstance(p, OpenAIProvider) and p.name == "openai_compatible"


def test_capability_overrides_flow_from_settings() -> None:
    s = Settings(
        llm=LLMSettings(
            provider="openrouter",
            model="vendor/odd-model",
            api_key="k-1234567890",
            capabilities={"supports_tools": False},
        )
    )  # type: ignore[arg-type]
    p = build_llm_provider(s, FakeSecrets({}))
    assert p is not None and not p.capabilities.supports_tools


def test_vision_provider_reuses_the_agent_key_for_the_same_vendor() -> None:
    s = Settings(
        llm=LLMSettings(provider="openrouter", model="a/b", api_key="shared-key-123456"),  # type: ignore[arg-type]
        vision=VisionSettings(provider="openrouter", model="c/d"),
    )
    v = build_vision_provider(s, FakeSecrets({}))
    assert (
        isinstance(v, OpenRouterProvider) and v.model == "c/d" and v._api_key == "shared-key-123456"
    )  # noqa: SLF001
    assert (
        build_vision_provider(Settings(vision=VisionSettings(provider="none")), FakeSecrets({}))
        is None
    )


def test_container_flags_unusable_models_and_disables_vision_for_text_only_models() -> None:
    from app.container import build_container
    from app.state.memory import InMemoryRepository

    s = Settings(
        env="test",
        log_json=False,
        llm=LLMSettings(
            provider="openrouter",
            model="x/y",
            api_key="k-1234567890",
            capabilities={"supports_tools": False},
        ),  # type: ignore[arg-type]
        vision=VisionSettings(
            provider="openrouter", model="meta-llama/llama-3-8b", api_key="v-1234567890"
        ),
    )  # type: ignore[arg-type]
    c = build_container(s, repo=InMemoryRepository(), enable_browser=False, enable_desktop=False)
    assert c.llm_problem and "tool/function calling" in c.llm_problem
    assert c.services.vision is None and any("does not support images" in w for w in c.warnings)
    assert "vision_capture" not in c.registry.names()


def test_secrets_never_appear_in_settings_dumps() -> None:
    s = Settings(
        llm=LLMSettings(provider="gemini", api_key="AIzaSy-super-secret-key-1234567890abcdef")
    )  # type: ignore[arg-type]
    assert "super-secret" not in repr(s) + s.model_dump_json()
