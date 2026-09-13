"""Optional provider-backed response generation for governed chat."""

from __future__ import annotations

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


async def generate_llm_reply(messages: list[dict[str, str]]) -> LLMResult | None:
    """Call an OpenAI-compatible provider when configured; return None for local mode."""
    api_key = settings.llm_api_key or settings.nvidia_api_key
    provider = settings.llm_provider
    if provider.lower() in {"", "mock", "local"} and settings.nvidia_api_key:
        provider = "nvidia"
    if provider.lower() in {"", "mock", "local"} or not api_key:
        return None
    started = time.perf_counter()
    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    choices = body.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM provider returned no assistant content")
    usage = body.get("usage") or {}
    return LLMResult(
        reply=content.strip(),
        provider=provider,
        model=settings.llm_model,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        cost_usd=float(usage.get("cost", 0.0) or 0.0),
    )
