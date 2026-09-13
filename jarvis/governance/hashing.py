"""Deterministic content hashes for SpiralTurn I/O and ledger chaining.

Canonicalization is stable across key order and Pydantic model vs dict
inputs so the same logical payload always hashes the same. Later phases
reuse :func:`chain_hash` / :func:`verify_chain_hash` for Audit Ledger
append and verify — do not introduce a second hashing scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

INTEGRITY_VERSION = "v1"
HASH_ALGORITHM = "sha256"


def _normalize(value: Any) -> Any:
    """Convert a value into JSON-stable primitives."""

    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    return value


def canonicalize(value: Any) -> bytes:
    """Return a stable UTF-8 JSON encoding of ``value``.

    Keys are sorted, separators are compact, and Unicode is preserved so
    the same logical structure always produces the same bytes.
    """

    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    """SHA-256 hex digest of the canonical encoding of ``value``."""

    digest = hashlib.sha256(canonicalize(value))
    return digest.hexdigest()


def chain_hash(
    previous_hash: str | None,
    payload: Any,
    *,
    integrity_version: str = INTEGRITY_VERSION,
) -> str:
    """Hash suitable for append-only ledger chaining.

    The digest covers integrity version, the previous event hash (or
    ``None`` for genesis), and the normalized payload. Changing any of
    those fields changes the current hash.
    """

    envelope = {
        "integrity_version": integrity_version,
        "payload": payload,
        "previous_event_hash": previous_hash,
    }
    return content_hash(envelope)


def verify_content_hash(value: Any, expected_hex: str) -> bool:
    """Return True if ``value`` hashes to ``expected_hex``."""

    actual = content_hash(value)
    return hmac.compare_digest(actual, expected_hex)


def verify_chain_hash(
    previous_hash: str | None,
    payload: Any,
    current_hash: str,
    *,
    integrity_version: str = INTEGRITY_VERSION,
) -> bool:
    """Return True if ``current_hash`` matches the chained digest."""

    expected = chain_hash(previous_hash, payload, integrity_version=integrity_version)
    return hmac.compare_digest(expected, current_hash)
