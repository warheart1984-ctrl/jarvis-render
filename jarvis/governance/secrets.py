"""Omit credentials and private reasoning from public governance views.

Audit Trace and Audit Ledger payloads must never carry API keys, auth
headers, hidden prompts, or raw private model reasoning. Continuity and
Reviver schemas store references, not secrets.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_SECRET_EXACT_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "cot",
        "credentials",
        "password",
        "secret",
        "token",
    }
)

_SECRET_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "apikey",
    "auth_header",
    "authorization",
    "bearer",
    "chain_of_thought",
    "credential",
    "hidden_prompt",
    "password",
    "private_cot",
    "private_key",
    "private_prompt",
    "private_reasoning",
    "refresh_token",
    "secret",
)


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def is_secret_key(key: str) -> bool:
    """Return True if ``key`` names a credential or private-reasoning field."""

    lowered = _normalize_key(str(key))
    if lowered in _SECRET_EXACT_KEYS:
        return True
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def omit_secrets(value: Any) -> Any:
    """Recursively drop secret-named keys from mappings and sequences."""

    if isinstance(value, BaseModel):
        return omit_secrets(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: omit_secrets(item) for key, item in value.items() if not is_secret_key(str(key))}
    if isinstance(value, list):
        return [omit_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [omit_secrets(item) for item in value]
    if hasattr(value, "__dict__"):
        raise TypeError(f"unsupported structured audit value: {type(value).__name__}")
    return value
