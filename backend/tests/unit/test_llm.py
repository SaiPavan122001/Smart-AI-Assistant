"""LLM providers against recorded wire formats (httpx.MockTransport): no network, no keys."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.config.settings import LimitSettings, LLMSettings, Settings
from app.llm.anthropic import AnthropicProvider
from app.llm.base import (
    ImageBlock,
    LLMRequest,
    LLMToolDef,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.llm.factory import build_llm_provider
from app.llm.gateway import LLMGateway
from app.llm.openai import OpenAIProvider
from app.llm.scripted import ScriptedProvider, structured, text, tool_use
from app.llm.structured import extract_json
from app.observability.usage import UsageMeter
from app.utils.control import CancelToken
from app.utils.errors import ErrorCode, LimitExceeded, LLMError, TaskCancelled
from pydantic import BaseModel

TOOLS = [
    LLMToolDef(
        name="fs_list",
        description="list",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ----------------------------------------------------------------------------- Anthropic
async def test_anthropic_request_and_response_mapping() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"], seen["body"] = dict(req.headers), json.loads(req.content)
        return httpx.Response(200, json={
            "model": "claude-x", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "listing"}, {"type": "tool_use", "id": "tu_1", "name": "fs_list", "input": {"path": "@Desktop"}}],
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100},
        })  # fmt: skip

    p = AnthropicProvider(
        api_key="sk-ant-test-key-1234567890", model="claude-x", client=client(handler)
    )
    req = LLMRequest(system="SYS", messages=[Message.user("hi"), Message(role="assistant", content=[ToolUseBlock(id="tu_0", name="fs_list", input={})]),
                     Message(role="user", content=[ToolResultBlock(tool_use_id="tu_0", content="ok", is_error=False)])], tools=TOOLS, tool_choice="any")  # fmt: skip
    resp = await p.complete(req)
    body = seen["body"]
    assert (
        seen["headers"]["x-api-key"] == "sk-ant-test-key-1234567890"
        and seen["headers"]["anthropic-version"]
    )
    assert body["tool_choice"] == {"type": "any"} and body["tools"][0]["name"] == "fs_list"
    assert body["system"][0]["cache_control"] == {
        "type": "ephemeral"
    }  # prompt caching on the stable system prompt
    assert (
        body["messages"][1]["content"][0]["type"] == "tool_use"
        and body["messages"][2]["content"][0]["type"] == "tool_result"
    )
    assert resp.text == "listing" and resp.tool_uses[0].input == {"path": "@Desktop"}
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cached_input_tokens) == (
        120,
        30,
        100,
    )


async def test_anthropic_structured_output_uses_forced_tool_and_images_map() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}
        assert body["messages"][0]["content"][0]["source"]["type"] == "base64"
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "emit_structured_output",
                        "input": {"a": 1},
                    }
                ],
                "usage": {},
            },
        )

    p = AnthropicProvider(api_key="k", model="m", client=client(handler))
    req = LLMRequest(
        system="s",
        messages=[
            Message(role="user", content=[ImageBlock(data_b64="AAAA"), TextBlock(text="look")])
        ],
        response_schema={"type": "object"},
    )
    assert (await p.complete(req)).structured == {"a": 1}


async def test_http_errors_retry_then_surface_without_leaking_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.llm.http.asyncio.sleep", lambda *_a, **_k: _noop())
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return (
            httpx.Response(
                529 if calls["n"] < 3 else 200,
                json={"content": [{"type": "text", "text": "ok"}], "usage": {}},
            )
            if calls["n"] >= 3
            else httpx.Response(529, text="overloaded")
        )

    p = AnthropicProvider(api_key="k", model="m", client=client(flaky))
    assert (
        await p.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    ).text == "ok" and calls["n"] == 3

    bad = AnthropicProvider(
        api_key="k",
        model="m",
        client=client(
            lambda r: httpx.Response(
                401, text='{"error":"invalid x-api-key sk-ant-leaked-key-1234567890"}'
            )
        ),
    )
    with pytest.raises(LLMError) as ei:
        await bad.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    assert (
        "sk-ant-leaked-key-1234567890" not in str(ei.value)
        and ei.value.details["status"] == 401
        and not ei.value.retryable
    )


async def _noop() -> None:
    return None


# ----------------------------------------------------------------------------- OpenAI-compatible
async def test_openai_mapping_tools_results_and_json_schema() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"], seen["body"] = req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={
            "model": "gpt-x", "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "fs_list", "arguments": '{"path": "@Desktop"}'}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        })  # fmt: skip

    p = OpenAIProvider(
        api_key="sk-test",
        model="gpt-x",
        base_url="http://localhost:11434/v1",
        client=client(handler),
    )
    req = LLMRequest(system="SYS", tools=TOOLS, tool_choice="any", messages=[
        Message.user("go"), Message(role="assistant", content=[TextBlock(text="calling"), ToolUseBlock(id="c0", name="fs_list", input={"path": "x"})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="c0", content="[]")])])  # fmt: skip
    resp = await p.complete(req)
    msgs = seen["body"]["messages"]
    assert seen["auth"] == "Bearer sk-test" and msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "fs_list" and msgs[3] == {
        "role": "tool",
        "tool_call_id": "c0",
        "content": "[]",
    }
    assert (
        seen["body"]["tool_choice"] == "required"
        and seen["body"]["tools"][0]["function"]["name"] == "fs_list"
    )
    assert resp.tool_uses[0].input == {"path": "@Desktop"} and resp.usage.input_tokens == 50

    def jhandler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}], "usage": {}},
        )

    q = OpenAIProvider(api_key="k", model="m", client=client(jhandler))
    assert (
        await q.complete(
            LLMRequest(system="s", messages=[Message.user("x")], response_schema={"type": "object"})
        )
    ).structured == {"ok": True}


def test_extract_json_is_lenient() -> None:
    assert extract_json('Sure! Here you go: {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("no json here") is None and extract_json("[1,2]") is None


def test_factory_selects_provider_and_never_requires_keys_in_repr() -> None:
    assert build_llm_provider(Settings(llm=LLMSettings(provider="none"))) is None
    s = Settings(llm=LLMSettings(provider="anthropic", api_key="sk-ant-secret-1234567890"))  # type: ignore[arg-type]
    assert isinstance(build_llm_provider(s), AnthropicProvider)
    assert "sk-ant-secret" not in repr(s) and "sk-ant-secret" not in s.model_dump_json()
    local = Settings(
        llm=LLMSettings(provider="openai", base_url="http://localhost:11434/v1", model="llama3")
    )
    assert isinstance(build_llm_provider(local), OpenAIProvider)


# ----------------------------------------------------------------------------- gateway
class Out(BaseModel):
    n: int


async def test_gateway_accounting_budget_and_redaction() -> None:
    llm = ScriptedProvider([text("a"), text("b")])
    meter = UsageMeter(LimitSettings(max_llm_calls=2), price_in_per_mtok=3, price_out_per_mtok=15)
    gw = LLMGateway(llm)
    req = LLMRequest(
        system="sys sk-ant-abcdefghijklmnopqrstuv",
        messages=[
            Message.user(
                "my password=hunter2xyz and token ghp_abcdefghijklmnopqrstuvwxyz0123456789"
            )
        ],
        purpose="plan",
    )
    await gw.complete(req, meter=meter, token=CancelToken())
    sent = llm.requests[0]
    assert (
        "hunter2xyz" not in sent.messages[0].content[0].text
        and "ghp_abcdef" not in sent.messages[0].content[0].text
    )  # type: ignore[union-attr]
    assert "sk-ant-abcdefghijklmnopqrstuv" not in sent.system, (
        "secrets are masked before leaving the process"
    )
    await gw.complete(req, meter=meter, token=CancelToken())
    assert (
        meter.usage.llm_calls == 2
        and meter.usage.input_tokens == 200
        and meter.usage.estimated_cost_usd > 0
    )
    with pytest.raises(LimitExceeded):
        await gw.complete(req, meter=meter, token=CancelToken())
    assert len(llm.requests) == 2, "no provider call once the budget is exhausted"


async def test_gateway_structured_validation_and_repair() -> None:
    llm = ScriptedProvider([structured({"n": "not-int"}), structured({"n": 3})])
    out = await LLMGateway(llm).complete_structured(
        LLMRequest(system="s", messages=[Message.user("x")]),
        Out,
        meter=UsageMeter(LimitSettings()),
        token=CancelToken(),
    )
    assert out.n == 3 and "validation" in llm.requests[1].messages[-1].content[0].text  # type: ignore[union-attr]
    with pytest.raises(LLMError) as ei:
        await LLMGateway(
            ScriptedProvider([structured({"n": "x"}), structured({"n": "y"})])
        ).complete_structured(
            LLMRequest(system="s", messages=[Message.user("x")]),
            Out,
            meter=UsageMeter(LimitSettings()),
            token=CancelToken(),
        )
    assert ei.value.code == ErrorCode.LLM_ERROR


async def test_gateway_respects_cancellation_and_vision_budget() -> None:
    tok = CancelToken()
    tok.cancel("stop")
    with pytest.raises(TaskCancelled):
        await LLMGateway(ScriptedProvider([text("x")])).complete(
            LLMRequest(system="s", messages=[Message.user("x")]),
            meter=UsageMeter(LimitSettings()),
            token=tok,
        )
    meter = UsageMeter(LimitSettings(max_vision_calls=1))
    gw = LLMGateway(ScriptedProvider([text("a"), text("b")]))
    await gw.complete(
        LLMRequest(system="s", messages=[Message.user("x")], purpose="vision"),
        meter=meter,
        token=CancelToken(),
    )
    assert meter.usage.vision_calls == 1 and meter.usage.llm_calls == 0
    with pytest.raises(LimitExceeded):
        await gw.complete(
            LLMRequest(system="s", messages=[Message.user("x")], purpose="vision"),
            meter=meter,
            token=CancelToken(),
        )


def test_tool_use_helpers() -> None:
    assert tool_use("fs_list", path="x").tool_uses[0].input == {"path": "x"}


async def test_quota_errors_are_not_retried() -> None:
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, json={"error": {"code": "insufficient_quota", "message": "no credits"}}
        )

    p = OpenAIProvider(api_key="k", model="m", client=client(h))
    with pytest.raises(LLMError) as ei:
        await p.complete(LLMRequest(system="s", messages=[Message.user("x")]))
    assert calls["n"] == 1 and not ei.value.retryable
