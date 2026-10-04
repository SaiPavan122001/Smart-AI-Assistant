# ADR-0010: Provider-agnostic LLM interface over plain HTTP

**Context.** No vendor lock-in; tool calling and structured output are required; keys must be handled safely; tests must not need network.

**Decision.** `LLMProvider.complete(LLMRequest) -> LLMResponse` with neutral message blocks (text, image, tool_use, tool_result). `AnthropicProvider` and `OpenAIProvider` (any OpenAI-compatible endpoint) use `httpx`
(retry with backoff on 408/429/5xx, redacted errors). Structured output is a forced tool call (Anthropic) or `json_schema` response format (OpenAI) with lenient JSON extraction and one schema-repair retry.
`LLMGateway` is the single choke point: budget guard, outbound redaction, cancellation, accounting, cost estimate. A `ScriptedProvider` test adapter drives deterministic tests/evals and is never selectable by configuration.

**Consequences.** + Swap or add providers by implementing one method; tests run offline. − Provider-specific features (extended thinking, native structured outputs) are not used; live-endpoint behaviour is verified against recorded wire formats only (see PROJECT_STATUS).
