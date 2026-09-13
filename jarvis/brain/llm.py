"""Optional provider-backed response generation for governed chat."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from jarvis.core.config import settings


@dataclass(frozen=True)
class LLMResult:
    reply: str
    provider: str
    model: str
    latency_ms: float
    cost_usd: float = 0.0
    cost_reported: bool = False


class ProviderError(RuntimeError):
    """Public-safe error; never propagate response bodies or credentials."""


def provider_config() -> tuple[str, str]:
    provider = settings.llm_provider.lower()
    if provider in {"", "mock", "local"} and settings.nvidia_api_key:
        return "nvidia", settings.nvidia_api_key
    return provider, settings.llm_api_key or (settings.nvidia_api_key if provider == "nvidia" else "")


async def generate_llm_reply(messages: list[dict[str, str]]) -> LLMResult | None:
    """Call an OpenAI-compatible provider when configured; return None for local mode."""
    provider, api_key = provider_config()
    if provider in {"", "mock", "local"}:
        return None
    if not api_key:
        raise ProviderError("The chat provider API key is missing.")
    started = time.perf_counter()
    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "temperature": settings.llm_temperature,
        "stream": False,
        "max_tokens": settings.llm_max_tokens,
    }
    if provider == "nvidia" and "nemotron-3.5-lightning" in settings.llm_model:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, follow_redirects=False) as client:
            response = await client.post(endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            if response.status_code != 200:
                raise ProviderError(f"Chat provider returned HTTP {response.status_code}.")
            body = response.json()
    except httpx.TimeoutException:
        raise ProviderError("Chat provider timed out; no answer was confirmed.") from None
    except (httpx.HTTPError, ValueError):
        raise ProviderError("Chat provider is unavailable or returned invalid JSON.") from None
    if not isinstance(body, dict):
        raise ProviderError("Chat provider returned an invalid response.")
    choices = body.get("choices") or []
    try:
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("Chat provider returned no assistant content.") from None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Chat provider returned no assistant content.")
    usage = body.get("usage") or {}
    cost = usage.get("cost") if isinstance(usage, dict) else None
    reported = isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
    return LLMResult(
        reply=content.strip(),
        provider=provider,
        model=settings.llm_model,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        cost_usd=float(cost) if reported else 0.0,
        cost_reported=reported,
    )
