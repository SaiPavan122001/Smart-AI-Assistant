# Adding an LLM provider

1. Implement `LLMProvider` (`backend/app/llm/base.py`):

```python
class MyProvider(LLMProvider):
    name = "my"
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None, timeout_s: float = 90, client: httpx.AsyncClient | None = None): ...
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
```

Map the neutral `LLMRequest` (system, messages made of `TextBlock/ImageBlock/ToolUseBlock/ToolResultBlock`, tools, `tool_choice`, `response_schema`) to your wire format and back to `LLMResponse`
(`text`, `tool_uses`, `structured`, `usage`, `latency_ms`). Requirements:

* **Tool calling**: `tool_choice="any"` must force a tool call; `"auto"/None` lets the model decide; a tool name forces that tool.
* **Structured output**: when `response_schema` is set, return the parsed object in `structured` (forced tool call, native JSON schema, or `extract_json(text)` as a fallback).
* **Errors**: use `app.llm.http.post_json` for retry/backoff and redacted errors, or raise `LLMError` yourself (never include keys in messages).
* **Usage**: fill `input_tokens/output_tokens` (cost accounting and budgets rely on them).
* Accept an injectable `httpx.AsyncClient` so tests can use `httpx.MockTransport`.

2. Register it with `register_provider(ProviderSpec(...))` in `llm/registry.py`, add the name to `ProviderName` in `config/settings.py`, and add model families to `llm/capabilities.py` if needed. Add a case to the cross-provider conformance test in `tests/unit/test_providers.py`. Document env vars in `.env.example` (a test checks every documented variable exists).
3. Test with recorded wire formats like `tests/unit/test_llm.py` (request mapping, response mapping, structured output, retry/error redaction).
4. Smoke-test against the real endpoint with your key before use: run a trivial task and the eval suite in *live* mode if you add one.

The vision path reuses the same interface (`ImageBlock`); `llm/factory.py::build_vision_provider` maps `vision.provider`.
