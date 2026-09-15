"""Observe-only nx_search backend.

Results are EvidenceRef / ToolCallRecord only — never authority, never memory admission.
Fail-closed on missing binary: UNAVAILABLE / degraded, never invent files.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Protocol

from jarvis.brain.tools.envelope import SourceReceipt, ToolCallRecord, ToolCallStatus, utc_now
from jarvis.core.config import settings


class NxSearchBackend(Protocol):
    def run(self, query: str, *, timeout_seconds: float) -> list[dict[str, Any]]:
        ...


class CliNxSearchBackend:
    def __init__(self, bin_path: str, node: str = "node", timeout_seconds: float = 8.0):
        self.bin_path = bin_path
        self.node = node
        self.timeout_seconds = timeout_seconds

    def run(self, query: str, *, timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        if not self.bin_path:
            raise RuntimeError("nx_search binary not configured")
        timeout = timeout_seconds or self.timeout_seconds
        try:
            # Minimal CLI contract: node <bin> --query "<query>"
            proc = subprocess.run(
                [self.node, self.bin_path, "--query", query],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("nx_search binary not found")
        except subprocess.TimeoutExpired:
            raise TimeoutError("nx_search timeout")
        if proc.returncode != 0:
            # Degrade, do not raise governance error
            return []
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []
        # Expect list of items with path / excerpt / etc.
        return data if isinstance(data, list) else []


class FakeNxSearchBackend:
    def __init__(self, hits: list[dict[str, Any]] | None = None):
        self.hits = hits or []

    def run(self, query: str, *, timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        return self.hits


def _make_receipt(item: dict[str, Any]) -> SourceReceipt:
    source_id = str(item.get("path") or item.get("id") or "nx-unknown")
    url = f"file://{source_id}"
    excerpt = str(item.get("excerpt") or item.get("snippet") or "")
    content = f"{source_id}:{excerpt}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return SourceReceipt(
        source_id=source_id[:120],
        url=url[:500],
        retrieved_at=utc_now(),
        excerpt=excerpt[:240],
        content_hash=content_hash,
    )


def run_nx_search(
    query: str,
    *,
    backend: NxSearchBackend | None = None,
    transaction_id: str,
    correlation_id: str,
    timeout_seconds: float | None = None,
) -> ToolCallRecord:
    """Run nx_search observe-only.

    Returns ToolCallRecord with bounded SourceReceipts. Never writes memory.
    """
    timeout = timeout_seconds or settings.observe_tool_timeout_seconds
    attempts = 1
    attempt = 1
    provider = "nx_search"
    model = "cli"
    latency_ms = 0.0
    error: str | None = None
    status = ToolCallStatus.ACCEPTED
    sources: list[SourceReceipt] = []

    # Resolve backend
    if backend is None:
        bin_path = settings.nx_search_bin
        node = settings.nx_search_node
        if not bin_path:
            status = ToolCallStatus.UNAVAILABLE
            error = "nx_search binary not configured"
            backend = None
        else:
            backend = CliNxSearchBackend(bin_path, node=node, timeout_seconds=timeout)

    if backend is None:
        raw_hits = []
    else:
        try:
            raw_hits = backend.run(query, timeout_seconds=timeout)
        except RuntimeError as exc:
            status = ToolCallStatus.UNAVAILABLE
            error = str(exc)
            raw_hits = []
        except TimeoutError:
            status = ToolCallStatus.TIMEOUT
            error = "nx_search timeout"
            raw_hits = []
        except Exception as exc:
            status = ToolCallStatus.DEGRADED
            error = f"nx_search error: {exc}"
            raw_hits = []

    # Cap hits
    limit = getattr(settings, "observe_nx_limit", 8)
    hits = raw_hits[:max(1, limit)]

    for item in hits:
        try:
            sources.append(_make_receipt(item))
        except Exception:
            continue

    # Build deterministic hashes
    source_hash = hashlib.sha256(json.dumps({"query": query}, sort_keys=True).encode()).hexdigest()
    result_hash = hashlib.sha256(json.dumps([s.model_dump() for s in sources], sort_keys=True).encode()).hexdigest()

    if status == ToolCallStatus.ACCEPTED and not sources:
        # Degraded but not unavailable — search ran with no hits
        status = ToolCallStatus.DEGRADED
        error = "nx_search returned no hits"

    return ToolCallRecord(
        tool_name="nx_search",
        arguments={"query": query},
        status=status,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
        timeout_seconds=timeout,
        attempts=attempts,
        attempt=attempt,
        retryable=status in {ToolCallStatus.TIMEOUT, ToolCallStatus.DEGRADED},
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        cost_usd=0.0,
        cost_reported=False,
        source_hash=source_hash,
        result_hash=result_hash,
        citations=[s.source_id for s in sources],
        sources=sources,
        observe_only=True,
        memory_written=False,
        memory_eligible=False,
        authority=False,
        error=error,
        promotion="not_shipped",
    )
