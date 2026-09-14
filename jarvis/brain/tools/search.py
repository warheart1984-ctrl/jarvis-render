"""Observe-only web search: retrieve and cite, never instruct, govern, or remember.

Retrieved pages are TOOL_EXTERNAL evidence. They cannot issue commands, change
governance state, or become memory. Search runs only on an explicit user request
or a gated ``search_query`` field — not on every question.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, assert_never
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from jarvis.brain.deliberation import EvidenceRef, admit_external_suggestion
from jarvis.brain.tools.envelope import (
    SourceReceipt,
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    stub_tool_call,
    utc_now,
)
from jarvis.core.config import settings
from jarvis.governance.hashing import content_hash

MAX_QUERY = 500
_SEARCH_PATTERNS = (
    re.compile(r"(?is)\bsearch the web(?:\s+for)?\s*[:\-]?\s*(.*)$"),
    re.compile(r"(?is)\bweb search(?:\s+for)?\s*[:\-]?\s*(.*)$"),
    re.compile(r"(?is)\bsearch online(?:\s+for)?\s*[:\-]?\s*(.*)$"),
    re.compile(r"(?is)\blook(?:\s+this)?\s+up\s*[:\-]?\s*(.+)$"),
    re.compile(r"(?is)\bsearch for\s*[:\-]?\s*(.+)$"),
)


class SearchBackend(Protocol):
    provider: str
    model: str

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        """Return raw hits with url/title/excerpt. Must not raise on empty results."""


class FakeSearchBackend:
    """Deterministic, provider-free test double. Never a live web page."""

    provider = "fake"
    model = "web-search-v0-fake"

    def __init__(self, hits: list[dict[str, str]] | None = None) -> None:
        self.hits = hits
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        self.calls.append(query)
        if self.hits is not None:
            return list(self.hits)[:max_results]
        clipped = query[:80]
        return [
            {
                "url": "https://example.test/observe-only",
                "title": f"Fake result for {clipped}",
                "excerpt": f"Deterministic fake excerpt about {clipped}. Not a live web page.",
            }
        ][:max_results]


class UnavailableSearchBackend:
    """Runtime degrade when no search provider/key is configured."""

    provider = "none"
    model = "none"

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        raise SearchUnavailable("web search is not configured")


class SearchUnavailable(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status: ToolCallStatus = ToolCallStatus.UNAVAILABLE):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class HttpSearchBackend:
    """Tavily or Brave JSON search over the existing httpx client."""

    def __init__(self, provider: str, base_url: str, api_key: str, timeout: float) -> None:
        self.provider = provider
        self.model = f"web-search-v0-{provider}"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        headers: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                if self.provider == "brave":
                    headers["X-Subscription-Token"] = self.api_key
                    response = await client.get(
                        self.base_url + "/res/v1/web/search",
                        params={"q": query, "count": max_results},
                        headers=headers,
                    )
                else:
                    # Tavily-compatible POST. Key is sent as configured by the vendor, never logged.
                    response = await client.post(
                        self.base_url + "/search",
                        json={
                            "api_key": self.api_key,
                            "query": query,
                            "max_results": max_results,
                            "include_answer": False,
                        },
                    )
                if response.status_code in {401, 403}:
                    raise SearchUnavailable("search provider refused credentials", retryable=False)
                if response.status_code != 200:
                    raise SearchUnavailable(
                        f"search provider returned HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                body = response.json()
        except httpx.TimeoutException as exc:
            raise SearchUnavailable("search provider timed out", retryable=True, status=ToolCallStatus.TIMEOUT) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable("search provider is unavailable or returned invalid JSON", retryable=True) from exc
        if not isinstance(body, dict):
            raise SearchUnavailable("search provider returned an invalid response", retryable=False)
        return _parse_hits(body, max_results)


def _parse_hits(body: dict[str, Any], max_results: int) -> list[dict[str, str]]:
    rows = body.get("results")
    if not isinstance(rows, list):
        web = body.get("web")
        rows = web.get("results") if isinstance(web, dict) else []
    if not isinstance(rows, list):
        return []
    hits: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("link") or "")
        title = str(row.get("title") or row.get("name") or "")
        excerpt = str(row.get("content") or row.get("description") or row.get("snippet") or "")
        if not _public_http_url(url):
            continue
        hits.append({"url": url[:500], "title": title, "excerpt": excerpt})
        if len(hits) >= max_results:
            break
    return hits


def _public_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def configured_search_backend() -> SearchBackend:
    provider = (settings.search_provider or "").strip().lower()
    if provider in {"fake", "mock"}:
        return FakeSearchBackend()
    if provider in {"", "none", "off", "disabled"}:
        return UnavailableSearchBackend()
    key = settings.search_api_key.strip()
    if not key:
        return UnavailableSearchBackend()
    base = settings.search_base_url.strip()
    if not base:
        base = "https://api.search.brave.com" if provider == "brave" else "https://api.tavily.com"
    return HttpSearchBackend(provider, base, key, settings.search_timeout_seconds)


def resolve_search_query(message: str, explicit: str | None = None) -> str | None:
    """Return a query only for an explicit search request. Never auto-retrieve."""

    if explicit is not None and explicit.strip():
        return explicit.strip()[:MAX_QUERY]
    text = (message or "").strip()
    if not text:
        return None
    for pattern in _SEARCH_PATTERNS:
        match = pattern.search(text)
        if match:
            captured = (match.group(1) or "").strip(" .?!:;-")
            query = captured or text
            return query[:MAX_QUERY]
    return None


def receipts_from_hits(hits: list[dict[str, str]], *, retrieved_at: str) -> list[SourceReceipt]:
    limit = settings.search_max_excerpt_chars
    receipts: list[SourceReceipt] = []
    for hit in hits:
        url = hit.get("url") or ""
        title = (hit.get("title") or "")[:120]
        excerpt = " ".join((hit.get("excerpt") or "").split())[:limit]
        if not excerpt:
            excerpt = title or url[:limit]
        digest = content_hash({"url": url, "excerpt": excerpt, "title": title})
        receipts.append(
            SourceReceipt(
                source_id=f"web-{digest[:16]}",
                url=url[:500],
                retrieved_at=retrieved_at,
                content_hash=digest,
                excerpt=excerpt,
                title=title,
            )
        )
    return receipts


def evidence_from_search_hit(receipt: SourceReceipt) -> EvidenceRef:
    """Admit a search hit as TOOL_EXTERNAL evidence. Authority is always false."""

    label = receipt.title or receipt.url
    summary = f"{label}: {receipt.excerpt}" if receipt.excerpt else label
    return admit_external_suggestion(
        source="web_search",
        summary=summary,
        evidence_id=receipt.source_id,
        requested_authority=True,
        match_text=receipt.excerpt,
        citation_id=receipt.source_id,
    )


def search_citation(receipt: SourceReceipt, *, session_id: str) -> dict[str, Any]:
    """Public receipt: ids, hashes, URL, time, bounded excerpt length — not a full page."""

    return {
        "source_type": "web_search",
        "source_id": receipt.source_id,
        "session_id": session_id,
        "url": receipt.url,
        "title": receipt.title,
        "retrieved_at": receipt.retrieved_at,
        "content_sha256": receipt.content_hash,
        "excerpt_sha256": content_hash(receipt.excerpt),
        "excerpt_chars": len(receipt.excerpt),
        "trust_status": receipt.trust_status,
        "citation_id": receipt.source_id,
    }


async def invoke_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    transaction_id: str,
    correlation_id: str,
) -> ToolCallRecord:
    try:
        tool = ToolName(name)
    except ValueError:
        return ToolCallRecord(
            tool_name=name[:80],
            arguments=arguments,
            status=ToolCallStatus.INVALID,
            transaction_id=transaction_id,
            correlation_id=correlation_id,
            error="unknown tool",
        )
    match tool:
        case ToolName.WEB_SEARCH:
            return await run_web_search(
                query=str(arguments.get("query") or ""),
                session_id="",
                tenant_id="",
                owner_sub="",
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                quota=lambda *_args: True,
            )
        case (
            ToolName.CALCULATOR
            | ToolName.CLOCK
            | ToolName.WEATHER
            | ToolName.DOCUMENT_RETRIEVAL
            | ToolName.HEALTH
        ):
            return stub_tool_call(
                tool, transaction_id=transaction_id, correlation_id=correlation_id, arguments=arguments
            )
        case _:
            assert_never(tool)


async def maybe_web_search(
    *,
    message: str,
    search_query: str | None,
    session_id: str,
    tenant_id: str,
    owner_sub: str,
    transaction_id: str,
    correlation_id: str,
    quota: Callable[[str, str, int, int], bool],
    backend: SearchBackend | None = None,
) -> ToolCallRecord | None:
    query = resolve_search_query(message, search_query)
    if query is None:
        return None
    return await run_web_search(
        query=query,
        session_id=session_id,
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
        quota=quota,
        backend=backend,
    )


async def run_web_search(
    *,
    query: str,
    session_id: str,
    tenant_id: str,
    owner_sub: str,
    transaction_id: str,
    correlation_id: str,
    quota: Callable[[str, str, int, int], bool],
    backend: SearchBackend | None = None,
) -> ToolCallRecord:
    started = time.perf_counter()
    envelope = {
        "tool_name": ToolName.WEB_SEARCH,
        "arguments": {"query": query},
        "query": query,
        "transaction_id": transaction_id or uuid4().hex,
        "correlation_id": correlation_id or uuid4().hex,
        "observe_only": True,
        "memory_written": False,
        "authority": False,
    }
    cleaned = query.strip()[:MAX_QUERY]
    if not cleaned:
        return ToolCallRecord(
            **envelope,
            status=ToolCallStatus.INVALID,
            error="search query is empty",
            latency_ms=_latency(started),
        )
    bucket = f"{tenant_id}:{owner_sub}:{session_id}"
    if session_id and not quota("web_search", bucket, settings.search_rate_limit, settings.search_rate_window_seconds):
        return ToolCallRecord(
            **envelope,
            status=ToolCallStatus.RATE_LIMITED,
            provider="internal",
            model="rate-limit",
            error="tenant/session search rate limit reached",
            latency_ms=_latency(started),
        )
    active = backend or configured_search_backend()
    attempts = settings.search_attempts
    timeout = settings.search_timeout_seconds
    last_error = "search unavailable"
    status = ToolCallStatus.UNAVAILABLE
    hits: list[dict[str, str]] = []
    for attempt in range(1, attempts + 1):
        try:
            async with asyncio.timeout(timeout):
                hits = await active.search(cleaned, max_results=settings.search_max_results)
            break
        except TimeoutError:
            last_error = "search timed out"
            status = ToolCallStatus.TIMEOUT
            if attempt >= attempts:
                return ToolCallRecord(
                    **envelope,
                    status=status,
                    provider=getattr(active, "provider", "none"),
                    model=getattr(active, "model", "none"),
                    error=last_error,
                    latency_ms=_latency(started),
                )
        except SearchUnavailable as exc:
            last_error = str(exc)
            status = exc.status
            if not exc.retryable or attempt >= attempts:
                return ToolCallRecord(
                    **envelope,
                    status=status,
                    provider=getattr(active, "provider", "none"),
                    model=getattr(active, "model", "none"),
                    error=last_error,
                    latency_ms=_latency(started),
                )
    receipts = receipts_from_hits(hits, retrieved_at=utc_now())[: settings.search_max_results]
    source_ids = [item.source_id for item in receipts]
    source_hash = content_hash(source_ids) if source_ids else ""
    result_hash = content_hash(
        {"query": cleaned, "source_ids": source_ids, "hashes": [item.content_hash for item in receipts]}
    )
    return ToolCallRecord(
        **envelope,
        status=ToolCallStatus.ACCEPTED if receipts else ToolCallStatus.DEGRADED,
        provider=getattr(active, "provider", "none"),
        model=getattr(active, "model", "none"),
        latency_ms=_latency(started),
        source_hash=source_hash,
        result_hash=result_hash,
        citations=source_ids,
        sources=receipts,
        error=None if receipts else "search returned no usable sources",
    )


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def quoted_search_payload(receipts: list[SourceReceipt]) -> list[dict[str, str]]:
    """Untrusted user-role payload. Never concatenated into system/governance prompts."""

    return [
        {
            "source_id": item.source_id,
            "url": item.url,
            "title": item.title,
            "excerpt": item.excerpt,
            "trust_status": item.trust_status,
        }
        for item in receipts
    ]

