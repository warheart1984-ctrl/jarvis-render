"""Bounded, provider-independent inference slots with explicit degraded outcomes."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from jarvis.core.config import MAX_INFERENCE_SLOTS, ProviderSlot, settings


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

    def __init__(
        self,
        message: str,
        retryable: bool = True,
        status: str = "unavailable",
        block_credentials: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.block_credentials = block_credentials


_model_cooldowns: dict[tuple[str, str, str], float] = {}

NVIDIA_INTEGRATE_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Chat-completions IDs currently listed on NVIDIA NIM (build.nvidia.com/models.md).
# Retired/deprecated Nano, Mini, and Llama-3.1 Nemotron hosted IDs are omitted.
# Lightning is usually primary and is deduped when appended.
NVIDIA_FALLBACK_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "meta/muse-glimmer-30b",
)


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


def _append_slot(
    slots: list[ProviderSlot],
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
) -> None:
    if len(slots) >= MAX_INFERENCE_SLOTS or not model:
        return
    if any(slot.provider == provider and slot.model == model and slot.base_url == base_url for slot in slots):
        return
    slots.append(
        ProviderSlot(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout_seconds=settings.llm_attempt_timeout_seconds,
        )
    )


def configured_slots() -> list[ProviderSlot]:
    if settings.llm_slots:
        return list(settings.llm_slots)[:MAX_INFERENCE_SLOTS]
    provider, _ = provider_config()
    if provider in {"", "mock", "local"}:
        return []
    slots: list[ProviderSlot] = []
    if provider == "nvidia":
        key_env = "NVIDIA_API_KEY" if not settings.llm_api_key else "JARVIS_LLM_API_KEY"
        fallbacks = [item.strip() for item in settings.llm_fallback_models.split(",") if item.strip()]
        for model in dict.fromkeys([settings.llm_model.strip(), *fallbacks]):
            _append_slot(
                slots,
                provider="nvidia",
                model=model,
                base_url=settings.llm_base_url,
                api_key_env=key_env,
            )
    else:
        _append_slot(
            slots,
            provider=provider,
            model=settings.llm_model.strip(),
            base_url=settings.llm_base_url,
            api_key_env="JARVIS_LLM_API_KEY",
        )
    if settings.nvidia_api_key:
        nvidia_base = settings.llm_base_url if provider == "nvidia" else NVIDIA_INTEGRATE_BASE_URL
        for model in NVIDIA_FALLBACK_MODELS:
            _append_slot(
                slots,
                provider="nvidia",
                model=model,
                base_url=nvidia_base,
                api_key_env="NVIDIA_API_KEY",
            )
    return slots


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
            if error.block_credentials:
                blocked_keys.add(slot.api_key_env)
                break
            if not error.retryable:
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


def _http_provider_error(status_code: int) -> ProviderError:
    auth_error = status_code in {401, 403}
    client_error = 400 <= status_code < 500 and status_code != 429
    return ProviderError(
        f"Chat provider returned HTTP {status_code}.",
        retryable=not (auth_error or client_error),
        block_credentials=auth_error,
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
                raise _http_provider_error(response.status_code)
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
    if re.search(r"<\s*/?\s*(?:think|analysis|reasoning)\s*>", content, re.IGNORECASE):
        raise ProviderError("Chat provider returned reasoning markup instead of a clean answer.", status="unknown")
    if choice.get("finish_reason") == "length":
        raise ProviderError("Chat provider returned a truncated answer.", status="unknown")
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
