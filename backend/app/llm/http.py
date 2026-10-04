"""Shared HTTP plumbing for LLM providers: retry with backoff, key-safe errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.utils.errors import LLMError
from app.utils.redact import redact_text

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = LLMError(f"LLM transport error: {type(exc).__name__}", retryable=True)
        else:
            if resp.status_code < 300:
                try:
                    return resp.json()  # type: ignore[no-any-return]
                except ValueError as exc:
                    raise LLMError("LLM returned a non-JSON response", retryable=False) from exc
            body = redact_text(resp.text[:500])
            err = LLMError(
                f"LLM HTTP {resp.status_code}: {body}",
                retryable=resp.status_code in _RETRYABLE,
                details={"status": resp.status_code},
            )
            quota = any(
                m in resp.text
                for m in ("insufficient_quota", "credit_balance", "billing_hard_limit")
            )
            if resp.status_code not in _RETRYABLE or quota:  # out of credit never succeeds on retry
                err.retryable = False
                raise err
            last = err
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                await asyncio.sleep(min(float(retry_after), 20.0))
                continue
        if attempt < max_attempts:
            await asyncio.sleep(base_delay_s * (2 ** (attempt - 1)))
    assert last is not None
    raise last
