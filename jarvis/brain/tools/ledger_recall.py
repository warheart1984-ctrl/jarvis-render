"""Observe-only Continuity Ledger recall: recall-before-invention.

Runs governed emr_recall before inference when a Continuity client is
configured, admitting retrieved memories as MEMORY-kind EvidenceRefs (ids +
hashes + bounded excerpts). It never writes, never grants authority, and
degrades on ledger unavailability — recall failure is not fail-closed.

The recall query is derived from the turn message, bounded, and read-only.
No automatic memory writes happen through this tool; tool records are
audited like web search and stay observe-only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.brain.deliberation import EvidenceKind, EvidenceRef
from jarvis.brain.tools.envelope import (
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    fence_untrusted_data,
    utc_now,
    validate_tool_arguments,
)
from jarvis.continuity import ContinuityLedgerClient
from jarvis.core.config import settings
from jarvis.governance.hashing import content_hash

MAX_QUERY = 500
MAX_MEMORY_ID = 128


class LedgerRecallBackend(Protocol):
    provider: str
    model: str

    async def recall(
        self,
        query: str,
        *,
        max_memories: int,
        intent: str,
        session_key: str,
    ) -> dict[str, Any]:
        """Return the raw emr_recall response. Must not raise on empty bundles."""


class FakeLedgerRecallBackend:
    """Deterministic, provider-free test double. Never HTTP, never a write."""

    provider = "fake"
    model = "emr-recall-v0-fake"

    def __init__(self, bundle: list[dict[str, Any]] | None = None, *, delay_seconds: float = 0) -> None:
        self.bundle = bundle
        self.delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []

    async def recall(
        self,
        query: str,
        *,
        max_memories: int,
        intent: str,
        session_key: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "max_memories": max_memories,
                "intent": intent,
                "session_key": session_key,
            }
        )
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        items = self.bundle if self.bundle is not None else _default_fake_bundle(query)
        return {
            "protocol": "emr-recall-v1",
            "bundle": list(items)[:max_memories],
            "abstained": False,
            "abstention_reason": None,
            "conflicts": [],
            "provenance": [],
            "recall_summary": ["fake ledger recall bundle"],
            "intent_resolved": {"operation": intent},
        }


class UnavailableLedgerRecallBackend:
    """Runtime degrade when no Continuity client is configured."""

    provider = "none"
    model = "none"

    async def recall(
        self,
        query: str,
        *,
        max_memories: int,
        intent: str,
        session_key: str,
    ) -> dict[str, Any]:
        raise LedgerRecallUnavailable("Continuity Ledger recall is not configured")


class HttpLedgerRecallBackend:
    """emr_recall over the configured Continuity Ledger client. Read-only."""

    def __init__(self, client: ContinuityLedgerClient) -> None:
        self.client = client
        self.provider = "continuity"
        self.model = "emr-recall-v1"

    async def recall(
        self,
        query: str,
        *,
        max_memories: int,
        intent: str,
        session_key: str,
    ) -> dict[str, Any]:
        return await self.client.recall(
            session_key,
            query,
            intent,
            max_memories=max_memories,
            session_key=session_key,
        )


class LedgerRecallUnavailable(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status: ToolCallStatus = ToolCallStatus.UNAVAILABLE):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class LedgerRecallOutcome:
    """Public tool record plus the memory citations and evidence to admit."""

    record: ToolCallRecord
    citations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    quotes: dict[str, Any] | None = None


def _default_fake_bundle(query: str) -> list[dict[str, Any]]:
    clipped = query[:80]
    return [
        {
            "memory_id": "mem_fake_recall_0001",
            "content": f"Deterministic fake governed memory about {clipped}. "
            "Recalled before invention; never a write and never authority.",
            "subject": "fake-recall",
            "type": "fact",
            "status": "verified",
            "confidence": 0.9,
            "activation": 0.7,
            "tags": ["fake", "recall"],
        }
    ]


def resolve_recall_query(message: str, explicit: str | None = None) -> str | None:
    """Return a bounded query for recall-before-invention.

    An explicit field wins; otherwise the whole (bounded) message is recalled
    on. Unlike web search, recall is automatic per turn — it is governed
    read-back, not a retrieval side-effect.
    """

    if explicit is not None and explicit.strip():
        return explicit.strip()[:MAX_QUERY]
    text = (message or "").strip()
    if not text:
        return None
    return text[:MAX_QUERY]


def receipts_from_recall_bundle(bundle: list[dict[str, Any]], *, retrieved_at: str) -> list[dict[str, Any]]:
    char_limit = settings.ledger_recall_max_excerpt_chars
    receipts: list[dict[str, Any]] = []
    for item in bundle:
        memory_id = str(item.get("memory_id") or "")
        if not memory_id:
            continue
        content = " ".join((item.get("content") or "").split()) or "no content"
        excerpt = content[:char_limit]
        subject = str(item.get("subject") or "general")[:120]
        digest = content_hash({"memory_id": memory_id, "content": excerpt, "subject": subject})
        receipts.append(
            {
                "source_id": f"ledger-{digest[:16]}",
                "memory_id": memory_id[:MAX_MEMORY_ID],
                "subject": subject,
                "excerpt": excerpt,
                "type": str(item.get("type") or "fact")[:32],
                "status": str(item.get("status") or "unverified")[:32],
                "confidence": float(item.get("confidence") or 0.0),
                "activation": float(item.get("activation") or 0.0),
                "retrieved_at": retrieved_at,
                "content_sha256": digest,
            }
        )
    return receipts


def evidence_from_recall_item(receipt: dict[str, Any]) -> EvidenceRef:
    """Admit a recalled governed memory as MEMORY evidence. Never authority.

    Evidence id matches ``cite-{citation_id}`` so the REFLECT-stage
    ``evidence_from_citation`` pass dedups instead of double-counting.
    """

    source_id = receipt["source_id"]
    label = receipt.get("subject") or "governed memory"
    excerpt = receipt.get("excerpt") or ""
    summary = f"{label}: {excerpt}" if excerpt else label
    return EvidenceRef(
        evidence_id=f"cite-{source_id}"[:80],
        kind=EvidenceKind.MEMORY,
        summary=summary,
        authority=False,
        citation_id=source_id,
        memory_id=receipt.get("memory_id"),
        source="continuity_ledger",
        admission="Continuity recall: governed memory admitted as evidence, observe-only, never authority",
        match_text=excerpt,
    )


def recall_citation(receipt: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    """Public provenance receipt: ids, hashes, memory id, subject — no secrets."""

    return {
        "source_type": "memory",
        "source_id": receipt["source_id"],
        "session_id": session_id,
        "memory_id": receipt["memory_id"],
        "citation_id": receipt["source_id"],
        "content_sha256": receipt["content_sha256"],
        "excerpt_sha256": content_hash(receipt["excerpt"]),
        "subject": receipt["subject"],
        "retrieved_at": receipt["retrieved_at"],
        "trust_status": "governed_ledger",
    }


def quoted_recall_payload(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Fenced untrusted data. Recalled text is data, never instructions."""

    return fence_untrusted_data(
        [
            {
                "memory_id": item["memory_id"],
                "subject": item["subject"],
                "content": item["excerpt"],
                "type": item["type"],
                "status": item["status"],
                "confidence": str(item["confidence"]),
                "activation": str(item["activation"]),
                "trust_status": "governed_ledger",
            }
            for item in receipts
        ]
    )


def _tool_identity(
    tool_name: str, arguments: dict[str, Any], transaction_id: str, correlation_id: str
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "arguments": arguments,
        "transaction_id": transaction_id,
        "correlation_id": correlation_id,
        "timeout_seconds": settings.ledger_recall_timeout_seconds,
        "attempts": settings.ledger_recall_attempts,
    }


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


async def maybe_ledger_recall(
    *,
    message: str,
    recall_query: str | None,
    session_id: str,
    tenant_id: str,
    owner_sub: str,
    transaction_id: str,
    correlation_id: str,
    quota: Callable[[str, str, int, int], bool],
    client: ContinuityLedgerClient | None = None,
    backend: LedgerRecallBackend | None = None,
) -> LedgerRecallOutcome | None:
    """Return recall outcome when configured and queryable; otherwise None.

    ``backend`` wins over ``client`` so tests can inject a fake without
    wiring a live Continuity client.
    """

    if not settings.observe_tools_enabled or not settings.ledger_recall_enabled:
        return None
    query = resolve_recall_query(message, recall_query)
    if query is None:
        return None
    if backend is None and client is None:
        # No Continuity configured for this principal: silent skip, no tool record.
        return None
    active: LedgerRecallBackend = backend or HttpLedgerRecallBackend(client)  # type: ignore[arg-type]
    return await run_ledger_recall(
        query=query,
        session_id=session_id,
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
        quota=quota,
        backend=active,
    )


async def run_ledger_recall(
    *,
    query: str,
    session_id: str,
    tenant_id: str,
    owner_sub: str,
    transaction_id: str,
    correlation_id: str,
    quota: Callable[[str, str, int, int], bool],
    backend: LedgerRecallBackend,
) -> LedgerRecallOutcome | None:
    started = time.perf_counter()
    intent = settings.ledger_recall_intent.strip() or "transform"
    max_memories = settings.ledger_recall_max_memories
    try:
        arguments = validate_tool_arguments(
            ToolName.LEDGER_RECALL.value,
            {"query": query, "intent": intent, "max_memories": max_memories},
        )
    except ValueError as exc:
        return LedgerRecallOutcome(
            record=ToolCallRecord(
                **_tool_identity(ToolName.LEDGER_RECALL.value, {"query": query}, transaction_id, correlation_id),
                status=ToolCallStatus.INVALID,
                attempt=0,
                retryable=False,
                error=str(exc),
                latency_ms=_latency(started),
            )
        )
    identity = _tool_identity(ToolName.LEDGER_RECALL.value, arguments, transaction_id, correlation_id)
    bucket = f"{tenant_id}:{owner_sub}:{session_id}"
    if session_id and not quota(
        "ledger_recall", bucket, settings.ledger_recall_rate_limit, settings.ledger_recall_rate_window_seconds
    ):
        return LedgerRecallOutcome(
            record=ToolCallRecord(
                **identity,
                status=ToolCallStatus.RATE_LIMITED,
                attempt=0,
                retryable=False,
                provider="internal",
                model="rate-limit",
                error="tenant/session ledger recall rate limit reached",
                latency_ms=_latency(started),
            )
        )
    attempts = settings.ledger_recall_attempts
    timeout = settings.ledger_recall_timeout_seconds
    last_error = "ledger recall unavailable"
    status = ToolCallStatus.UNAVAILABLE
    retryable = False
    bundle: list[dict[str, Any]] = []
    last_attempt = 0
    for attempt in range(1, attempts + 1):
        last_attempt = attempt
        try:
            async with asyncio.timeout(timeout):
                response = await backend.recall(
                    query,
                    max_memories=max_memories,
                    intent=intent,
                    session_key=session_id,
                )
            raw_bundle = response.get("bundle") if isinstance(response, dict) else None
            if not isinstance(raw_bundle, list):
                return LedgerRecallOutcome(
                    record=ToolCallRecord(
                        **identity,
                        status=ToolCallStatus.DEGRADED,
                        attempt=attempt,
                        retryable=False,
                        provider=backend.provider,
                        model=backend.model,
                        error="ledger recall returned an invalid response",
                        latency_ms=_latency(started),
                    )
                )
            bundle = [item for item in raw_bundle if isinstance(item, dict)]
            break
        except TimeoutError:
            last_error = "ledger recall timed out"
            status = ToolCallStatus.TIMEOUT
            retryable = True
            if attempt >= attempts:
                return LedgerRecallOutcome(
                    record=ToolCallRecord(
                        **identity,
                        status=status,
                        attempt=attempt,
                        retryable=retryable,
                        provider=backend.provider,
                        model=backend.model,
                        error=last_error,
                        latency_ms=_latency(started),
                    )
                )
        except LedgerRecallUnavailable as exc:
            last_error = str(exc)
            status = exc.status
            retryable = exc.retryable
            if not exc.retryable or attempt >= attempts:
                return LedgerRecallOutcome(
                    record=ToolCallRecord(
                        **identity,
                        status=status,
                        attempt=attempt,
                        retryable=retryable,
                        provider=getattr(backend, "provider", "none"),
                        model=getattr(backend, "model", "none"),
                        error=last_error,
                        latency_ms=_latency(started),
                    )
                )
    receipts = receipts_from_recall_bundle(bundle, retrieved_at=utc_now())
    source_ids = [item["source_id"] for item in receipts]
    source_hash = content_hash(source_ids) if source_ids else ""
    result_hash = content_hash(
        {"query": arguments["query"], "source_ids": source_ids, "hashes": [item["content_sha256"] for item in receipts]}
    )
    accepted = bool(receipts)
    evidence = [evidence_from_recall_item(item) for item in receipts]
    record = ToolCallRecord(
        **identity,
        status=ToolCallStatus.ACCEPTED if accepted else ToolCallStatus.DEGRADED,
        attempt=max(last_attempt, 1),
        retryable=False,
        provider=getattr(backend, "provider", "none"),
        model=getattr(backend, "model", "none"),
        latency_ms=_latency(started),
        source_hash=source_hash if accepted else "",
        result_hash=result_hash if accepted else "",
        citations=[item["memory_id"] for item in receipts],
        sources=[],
        error=None if accepted else "ledger recall returned no usable memories",
    )
    if not accepted:
        return LedgerRecallOutcome(record=record)
    return LedgerRecallOutcome(
        record=record,
        citations=[recall_citation(item, session_id=session_id) for item in receipts],
        evidence=evidence,
        quotes=quoted_recall_payload(receipts),
    )
