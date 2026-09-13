"""Bounded, provider-independent inference slots with explicit degraded outcomes."""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from jarvis.core.config import ProviderSlot, settings


@dataclass(frozen=True)
class LLMResult:
    reply: str
    provider: str
    model: str
    latency_ms: float
    cost_usd: float = 0.0
    cost_reported: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    safe_mode: bool = False
    inference_status: str = "accepted"
    transaction_id: str = ""
    correlation_id: str = ""


class ProviderError(RuntimeError):
    """Public-safe error; never propagate provider bodies or credentials."""

    def __init__(self, message: str, retryable: bool = True, status: str = "unavailable"):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


_model_cooldowns: dict[tuple[str, str, str], float] = {}


def _slot_key(slot: ProviderSlot) -> str:
    if slot.api_key_env == "NVIDIA_API_KEY":
        return settings.nvidia_api_key
    if slot.api_key_env == "JARVIS_LLM_API_KEY":
        return settings.llm_api_key
    return os.environ.get(slot.api_key_env, "") if slot.api_key_env else ""


def provider_config() -> tuple[str, str]:
    if settings.llm_slots:
        slot = settings.llm_slots[0]
        return slot.provider, _slot_key(slot)
    provider = settings.llm_provider.lower()
    if provider in {"", "mock", "local"} and settings.nvidia_api_key:
        return "nvidia", settings.nvidia_api_key
    return provider, settings.llm_api_key or (settings.nvidia_api_key if provider == "nvidia" else "")


def configured_slots() -> list[ProviderSlot]:
    if settings.llm_slots:
        return settings.llm_slots
    provider, _ = provider_config()
    if provider in {"", "mock", "local"}:
        return []
    fallbacks = settings.llm_fallback_models.split(",") if provider == "nvidia" else []
    models = list(dict.fromkeys(m.strip() for m in [settings.llm_model, *fallbacks] if m.strip()))[:3]
    key_env = "NVIDIA_API_KEY" if provider == "nvidia" and not settings.llm_api_key else "JARVIS_LLM_API_KEY"
    return [
        ProviderSlot(
            provider=provider,
            model=m,
            base_url=settings.llm_base_url,
            api_key_env=key_env,
            timeout_seconds=settings.llm_attempt_timeout_seconds,
        )
        for m in models
    ]


def configured_models() -> list[str]:
    return [slot.model for slot in configured_slots()]


def inference_configured() -> bool:
    return any(not slot.api_key_env or bool(_slot_key(slot)) for slot in configured_slots())


async def generate_llm_reply(
    messages: list[dict[str, str]],
    *,
    transaction_id: str = "",
    correlation_id: str = "",
    audit_attempt: Callable[[dict[str, Any]], None] | None = None,
) -> LLMResult | None:
    slots = configured_slots()
    if not slots:
        return None  # Explicit local development mode; not an outage substitution.
    transaction_id = transaction_id or uuid4().hex
    correlation_id = correlation_id or uuid4().hex
    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []

    def record(entry: dict[str, Any]) -> None:
        if audit_attempt:
            audit_attempt({**entry, "phase": "completed"})
        attempts.append(entry)

    blocked_keys: set[str] = set()
    outcome = "unavailable"
    for old_key, until in list(_model_cooldowns.items()):
        if until <= time.monotonic():
            _model_cooldowns.pop(old_key, None)
    for slot_index, slot in enumerate(slots):
        key = (slot.provider, slot.base_url, slot.model)
        envelope = {
            "transaction_id": transaction_id,
            "correlation_id": correlation_id,
            "provider": slot.provider,
            "model": slot.model,
            "slot": slot_index + 1,
        }
        api_key = _slot_key(slot)
        skipped = None
        if slot.api_key_env and (not api_key or slot.api_key_env in blocked_keys):
            skipped = "credential_unavailable"
        elif _model_cooldowns.get(key, 0) > time.monotonic():
            skipped = "cooldown"
        if skipped:
            record(
                {
                    **envelope,
                    "attempt": 0,
                    "status": "unavailable",
                    "reason": skipped,
                    "latency_ms": 0,
                    "attempted": False,
                }
            )
            continue
        slot_failed = False
        for attempt in range(1, slot.attempts + 1):
            remaining = settings.llm_timeout_seconds - (time.perf_counter() - started)
            if remaining <= 0:
                record(
                    {
                        **envelope,
                        "attempt": 0,
                        "status": "unavailable",
                        "reason": "budget_exhausted",
                        "latency_ms": 0,
                        "attempted": False,
                    }
                )
                break
            attempt_started = time.perf_counter()
            if audit_attempt:
                audit_attempt({**envelope, "attempt": attempt, "phase": "started", "status": "unknown"})
            try:
                async with asyncio.timeout(min(slot.timeout_seconds, remaining)):
                    result = await _request_model(messages, slot, api_key)
            except TimeoutError:
                error = ProviderError("Chat provider timed out; no answer was confirmed.")
            except ProviderError as exc:
                error = exc
            else:
                record(
                    {
                        **envelope,
                        "attempt": attempt,
                        "status": "accepted",
                        "latency_ms": result.latency_ms,
                        "attempted": True,
                    }
                )
                return LLMResult(
                    reply=result.reply,
                    provider=slot.provider,
                    model=slot.model,
                    latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    cost_usd=result.cost_usd,
                    cost_reported=result.cost_reported and len(attempts) == 1,
                    attempts=attempts,
                    fallback_used=slot_index > 0,
                    transaction_id=transaction_id,
                    correlation_id=correlation_id,
                )
            slot_failed = True
            outcome = error.status
            record(
                {
                    **envelope,
                    "attempt": attempt,
                    "status": outcome,
                    "reason": str(error),
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 3),
                    "attempted": True,
                }
            )
            if outcome == "refused":
                # Never use another provider to evade a content/safety refusal.
                break
            if not error.retryable:
                blocked_keys.add(slot.api_key_env)
                break
        if outcome == "refused":
            break
        if slot_failed:
            _model_cooldowns[key] = time.monotonic() + slot.cooldown_seconds
    record(
        {
            "transaction_id": transaction_id,
            "correlation_id": correlation_id,
            "provider": "internal",
            "model": "minimal-chat",
            "attempt": 1,
            "status": "accepted",
            "kind": "safe_response_only",
            "inference_confirmed": False,
            "latency_ms": 0,
        }
    )
    reply = (
        "I can’t fulfill that request. We can discuss a safe alternative in text-only mode."
        if outcome == "refused"
        else "I’m here in text-only safe mode. Inference providers are unavailable, so this is a basic status reply, "
        "not an LLM answer. You can keep typing; this conversation stays in Jarvis’s local history. "
        "I won’t execute actions or claim that a task succeeded. You can try your question again shortly."
    )
    return LLMResult(
        reply,
        "internal",
        "minimal-chat",
        round((time.perf_counter() - started) * 1000, 3),
        attempts=attempts,
        fallback_used=True,
        safe_mode=True,
        inference_status=outcome,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
    )


async def _request_model(messages: list[dict[str, str]], slot: ProviderSlot, api_key: str) -> LLMResult:
    started = time.perf_counter()
    payload: dict[str, Any] = {
        "model": slot.model,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "temperature": settings.llm_temperature,
        "stream": False,
        "max_tokens": settings.llm_max_tokens,
    }
    if slot.provider == "nvidia" and "nemotron" in slot.model:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=slot.timeout_seconds, follow_redirects=False) as client:
            response = await client.post(slot.base_url + "/chat/completions", headers=headers, json=payload)
            if response.status_code != 200:
                raise ProviderError(
                    f"Chat provider returned HTTP {response.status_code}.",
                    retryable=response.status_code not in {401, 403},
                )
            body = response.json()
    except httpx.TimeoutException:
        raise ProviderError("Chat provider timed out; no answer was confirmed.") from None
    except (httpx.HTTPError, ValueError):
        raise ProviderError("Chat provider is unavailable or returned invalid JSON.", status="unknown") from None
    if not isinstance(body, dict):
        raise ProviderError("Chat provider returned an invalid response.", status="unknown")
    try:
        choice = body["choices"][0]
        message = choice["message"]
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise ProviderError("Provider declined this request.", retryable=False, status="refused")
        content = message["content"]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("Chat provider returned no assistant content.", status="unknown") from None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Chat provider returned no assistant content.", status="unknown")
    usage = body.get("usage") or {}
    cost = usage.get("cost") if isinstance(usage, dict) else None
    reported = isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
    return LLMResult(
        content.strip(),
        slot.provider,
        slot.model,
        round((time.perf_counter() - started) * 1000, 3),
        cost_usd=float(cost) if reported else 0.0,
        cost_reported=reported,
    )
