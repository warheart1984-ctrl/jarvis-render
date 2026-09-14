"""Local clock: timezone-aware UTC timestamp. No network, no weather, no memory.

Runs only on an explicit time request. UTC is the honest v0 clock; this is not
a weather lookup and does not convert arbitrary IANA zones.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from jarvis.brain.deliberation import EvidenceRef, admit_external_suggestion
from jarvis.brain.tools.envelope import (
    LOCAL_TIMEOUT_SECONDS,
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    validate_tool_arguments,
)
from jarvis.governance.hashing import content_hash

_CLOCK_PATTERNS = (
    re.compile(r"(?is)\bwhat time is it\b"),
    re.compile(r"(?is)\bwhat(?:'s| is) the (?:current )?(?:utc )?time\b"),
    re.compile(r"(?is)\btell me the (?:current )?(?:utc )?time\b"),
    re.compile(r"(?is)\bcurrent (?:utc )?time\b"),
    re.compile(r"(?is)\b(?:utc|unix|current) timestamp\b"),
)


def resolve_clock_request(message: str) -> bool:
    """True only for an explicit time/clock request. Never auto-run. Not weather."""

    text = (message or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _CLOCK_PATTERNS)


def evidence_from_clock(record: ToolCallRecord) -> EvidenceRef | None:
    if record.status is not ToolCallStatus.ACCEPTED:
        return None
    iso = str(record.result.get("iso8601") or "")
    summary = f"UTC time {iso}"
    citation_id = record.citations[0] if record.citations else f"clock-{record.result_hash[:16]}"
    return admit_external_suggestion(
        source="clock",
        summary=summary,
        evidence_id=citation_id,
        requested_authority=True,
        match_text=summary,
        citation_id=citation_id,
    )


def clock_citation(record: ToolCallRecord, *, session_id: str) -> dict[str, Any]:
    citation_id = record.citations[0] if record.citations else f"clock-{record.result_hash[:16]}"
    return {
        "source_type": "clock",
        "source_id": citation_id,
        "session_id": session_id,
        "timezone": record.result.get("timezone") or "UTC",
        "iso8601": record.result.get("iso8601"),
        "unix_seconds": record.result.get("unix_seconds"),
        "content_sha256": record.result_hash,
        "trust_status": "local_deterministic",
        "citation_id": citation_id,
    }


def _identity(arguments: dict[str, Any], transaction_id: str, correlation_id: str) -> dict[str, Any]:
    return {
        "tool_name": ToolName.CLOCK.value,
        "arguments": arguments,
        "transaction_id": transaction_id,
        "correlation_id": correlation_id,
        "timeout_seconds": LOCAL_TIMEOUT_SECONDS,
        "attempts": 1,
        "provider": "local",
        "model": "utc-clock-v0",
    }


async def maybe_clock(
    *,
    message: str,
    transaction_id: str,
    correlation_id: str,
) -> ToolCallRecord | None:
    if not resolve_clock_request(message):
        return None
    return await run_clock(transaction_id=transaction_id, correlation_id=correlation_id)


async def run_clock(*, transaction_id: str, correlation_id: str) -> ToolCallRecord:
    started = time.perf_counter()
    try:
        arguments = validate_tool_arguments(ToolName.CLOCK.value, {"timezone": "UTC"})
    except ValueError as exc:
        return ToolCallRecord(
            **_identity({"timezone": "UTC"}, transaction_id, correlation_id),
            status=ToolCallStatus.INVALID,
            attempt=0,
            retryable=False,
            error=str(exc),
            latency_ms=_latency(started),
        )
    now = datetime.now(timezone.utc)
    result = {
        "timezone": "UTC",
        "iso8601": now.isoformat(),
        "unix_seconds": int(now.timestamp()),
    }
    digest = content_hash(result)
    return ToolCallRecord(
        **_identity(arguments, transaction_id, correlation_id),
        status=ToolCallStatus.ACCEPTED,
        attempt=1,
        retryable=False,
        latency_ms=_latency(started),
        source_hash=content_hash(arguments),
        result_hash=digest,
        citations=[f"clock-{digest[:16]}"],
        result=result,
    )


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
