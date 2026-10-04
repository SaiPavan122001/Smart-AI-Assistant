# ADR-0010: Provider-agnostic LLM interface over plain HTTP

**Context.** No vendor lock-in; the user picks the provider and model; tool calling and structured output are required; keys must be handled safely; tests must not need network.

**Decision.**
* `LLMProvider.complete(LLMRequest) -> LLMResponse` with neutral blocks (text, image, tool_use, tool_result) and a neutral `LLMUsage`/`LLMError`. Vendor translation lives ONLY in adapters (`openai`, `anthropic`, `gemini`, `openrouter` = OpenAI wire + headers, `openai_compatible` = OpenAI wire + user base URL). Adapters contain no planner, routing or tool logic.
* A provider **registry** (`llm/registry.py`) maps names to builders and key env vars; `llm/factory.py` builds the agent and vision providers from settings + the SecretProvider chain.
* A **ModelCapabilities** registry (`llm/capabilities.py`): known families, conservative defaults (vision never assumed), user overrides (`SAA_LLM__CAPABILITIES`). `require_for_agent` runs at startup (`Container.llm_problem`) and again before each task: no tool support, no system prompt or no structured output means the task is rejected with an explanation; no native JSON schema means a validated compatibility mode (schema instruction + lenient extract + repair); a text-only vision model disables vision with a warning.
* We deliberately do NOT use vendor computer-use tools. The model only ever returns normalized `ToolUse`; execution is always Runtime -> LLM -> ToolCall -> Router -> schema validation -> Permission Engine -> Security Policy -> Tool -> observation -> verification.
* `LLMGateway` stays the single choke point (budget, outbound redaction, cancellation, accounting). `ScriptedProvider` is test-only.
* `python -m app llm-check [--vision]` runs live conformance probes (plain, forced tool call, tool-result round trip, structured output, image).

**Consequences.** + Swapping providers is a configuration change; behaviour is verified by one cross-provider conformance suite. - Only OpenRouter/openai/gpt-4.1 has been verified live; Anthropic, Gemini, OpenAI-direct and generic OpenAI-compatible are verified against wire-format mocks only. Run `llm-check` with your own key before relying on them.
