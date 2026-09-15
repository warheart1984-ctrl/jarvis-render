"""Observe-only web search: cite provider snippets, never instruct, govern, or remember.

v0 consumes the search provider's JSON only (Tavily, Brave, or a fake). It does
not GET target pages. SSRF-on-fetch of hit URLs is therefore not the live
surface; any later fetch-this-URL path must add SSRF protections before it ships.

Provider snippets are TOOL_EXTERNAL evidence. They cannot issue commands, change
governance state, or become memory. Search runs only on an explicit user request
or a gated ``search_query`` field — not on every question.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from jarvis.brain.deliberation import EvidenceRef, admit_external_suggestion
from jarvis.brain.tools.envelope import (
    SourceReceipt,
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
    """Deterministic, provider-free test double. Never HTTP, never a live web page."""

    provider = "fake"
    model = "web-search-v0-fake"

    def __init__(self, hits: list[dict[str, str]] | None = None, *, delay_seconds: float = 0) -> None:
        self.hits = hits
        self.delay_seconds = delay_seconds
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        self.calls.append(query)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
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
    """Tavily or Brave JSON search. HTTP is only sent to the configured provider ``base_url``.

    Result hit URLs are parsed from the JSON body and never requested. Do not add
    a page-fetch path here without SSRF protections (resolve-and-check IP, block
    loopback/link-local/RFC1918/ULA, metadata hosts, credentials, non-http(s)).
    """

    def __init__(self, provider: str, base_url: str, api_key: str, timeout: float) -> None:
        self.provider = provider
        self.model = f"web-search-v0-{provider}"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _provider_endpoint(self, path: str) -> str:
        """Build a URL that stays on the configured search provider host."""

        expected = urlsplit(self.base_url)
        target = self.base_url + path
        actual = urlsplit(target)
        if not expected.hostname or actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise SearchUnavailable(
                "refusing to call a host other than the configured search provider",
                retryable=False,
            )
        return target

    async def search(self, query: str, *, max_results: int) -> list[dict[str, str]]:
        headers: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                if self.provider == "brave":
                    headers["X-Subscription-Token"] = self.api_key
                    response = await client.get(
                        self._provider_endpoint("/res/v1/web/search"),
                        params={"q": query, "count": max_results},
                        headers=headers,
                    )
                else:
                    # Tavily-compatible POST. Key is sent as configured by the vendor, never logged.
                    response = await client.post(
                        self._provider_endpoint("/search"),
                        json={
                            "api_key": self.api_key,
                            "query": query,
                            "max_results": max_results,
                            "include_answer": False,
                        },
                    )
                if 400 <= response.status_code < 500:
                    raise SearchUnavailable(
                        f"search provider returned HTTP {response.status_code}",
                        retryable=False,
                    )
                if len(response.content) > settings.search_max_response_bytes:
                    raise SearchUnavailable("search provider response exceeded max bytes", retryable=False)
                if response.status_code != 200:
                    raise SearchUnavailable(
                        f"search provider returned HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                body = response.json()
        except httpx.TimeoutException as exc:
            raise SearchUnavailable("search provider timed out", retryable=True, status=ToolCallStatus.TIMEOUT) from exc
        except SearchUnavailable:
            raise
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
        if not url_permitted(url):
            continue
        hits.append({"url": url[:500], "title": title, "excerpt": excerpt})
        if len(hits) >= max_results:
            break
    return hits


def _split_hosts(value: str) -> list[str]:
    return [item.strip().lower().lstrip(".") for item in value.split(",") if item.strip()]


def _host_matches(host: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP literal. Hostnames are not DNS-resolved (v0 never fetches hit URLs)."""

    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        if host.isdigit() and len(host) <= 10:
            try:
                n = int(host, 10)
                if 0 <= n <= 0xFFFFFFFF:
                    return ipaddress.IPv4Address(n)
            except (ValueError, ipaddress.AddressValueError):
                return None
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _non_global_ip_literal(host: str) -> bool:
    """Reject IP literals that are not globally routable.

    Numeric policy (stdlib ``ipaddress``, not hostname vibes): RFC1918, loopback,
    link-local (including 169.254.0.0/16), unspecified, multicast, reserved,
    IPv6 unique-local / loopback, and IPv4-mapped forms of those ranges.
    """

    ip = _ip_literal(host)
    return ip is not None and not ip.is_global


def url_permitted(url: str) -> bool:
    """HTTP(S) only, deny-list wins, optional allow-list, no non-global IP literals.

    Used to decide whether a provider-JSON hit URL may enter the evidence corpus.
    This is not a fetch allow-list: Jarvis does not retrieve target pages.
    """

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    deny = _split_hosts(settings.search_deny_hosts)
    allow = _split_hosts(settings.search_allow_hosts)
    if host in {"localhost"} or _non_global_ip_literal(host) or _host_matches(host, deny):
        return False
    if allow and not _host_matches(host, allow):
        return False
    return True


def _clip_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    clipped = raw[:max_bytes]
    return clipped.decode("utf-8", errors="ignore")


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
    char_limit = settings.search_max_excerpt_chars
    byte_limit = settings.search_max_bytes
    receipts: list[SourceReceipt] = []
    for hit in hits:
        url = hit.get("url") or ""
        if not url_permitted(url):
            continue
        title = _clip_bytes((hit.get("title") or "")[:120], byte_limit)
        excerpt = _clip_bytes(" ".join((hit.get("excerpt") or "").split()), byte_limit)[:char_limit]
        if not excerpt:
            excerpt = (title or url)[:char_limit]
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
        if len(receipts) >= settings.search_max_results:
            break
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
    timeout = settings.search_timeout_seconds
    attempts = settings.search_attempts
    try:
        tool = ToolName(name)
        validated = validate_tool_arguments(name, arguments)
    except ValueError as exc:
        return ToolCallRecord(
            tool_name=(name or "unknown")[:80] or "unknown",
            arguments=dict(arguments or {}),
            status=ToolCallStatus.INVALID,
            transaction_id=transaction_id,
            correlation_id=correlation_id,
            timeout_seconds=timeout,
            attempts=attempts,
            attempt=0,
            retryable=False,
            error=str(exc),
        )
    match tool:
        case ToolName.WEB_SEARCH:
            return await run_web_search(
                query=str(validated.get("query") or ""),
                session_id="",
                tenant_id="",
                owner_sub="",
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                quota=lambda *_args: True,
            )
        case ToolName.LEDGER_RECALL:
            from jarvis.brain.tools.ledger_recall import (
                HttpLedgerRecallBackend,
                UnavailableLedgerRecallBackend,
                run_ledger_recall,
            )

            client = (
                ContinuityLedgerClient(settings.continuity_ledger_url, settings.continuity_ledger_token, timeout=4.0)
                if settings.continuity_ledger_url
                else None
            )
            backend = HttpLedgerRecallBackend(client) if client is not None else UnavailableLedgerRecallBackend()
            outcome = await run_ledger_recall(
                query=str(validated.get("query") or ""),
                session_id="",
                tenant_id="",
                owner_sub="",
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                quota=lambda *_args: True,
                backend=backend,
            )
            return outcome.record
        case (
            ToolName.CALCULATOR
            | ToolName.CLOCK
            | ToolName.WEATHER
            | ToolName.DOCUMENT_RETRIEVAL
            | ToolName.HEALTH
        ):
            return stub_tool_call(
                tool,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                arguments=validated,
                timeout_seconds=timeout,
                attempts=attempts,
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


def _tool_identity(
    tool_name: str, arguments: dict[str, Any], transaction_id: str, correlation_id: str
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "arguments": arguments,
        "transaction_id": transaction_id,
        "correlation_id": correlation_id,
        "timeout_seconds": settings.search_timeout_seconds,
        "attempts": settings.search_attempts,
    }


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
    try:
        arguments = validate_tool_arguments(ToolName.WEB_SEARCH.value, {"query": query})
    except ValueError as exc:
        return ToolCallRecord(
            **_tool_identity(ToolName.WEB_SEARCH.value, {"query": query}, transaction_id, correlation_id),
            status=ToolCallStatus.INVALID,
            attempt=0,
            retryable=False,
            error=str(exc),
            latency_ms=_latency(started),
        )
    identity = _tool_identity(ToolName.WEB_SEARCH.value, arguments, transaction_id, correlation_id)
    bucket = f"{tenant_id}:{owner_sub}:{session_id}"
    if session_id and not quota("web_search", bucket, settings.search_rate_limit, settings.search_rate_window_seconds):
        return ToolCallRecord(
            **identity,
            status=ToolCallStatus.RATE_LIMITED,
            attempt=0,
            retryable=False,
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
    retryable = False
    hits: list[dict[str, str]] = []
    last_attempt = 0
    for attempt in range(1, attempts + 1):
        last_attempt = attempt
        try:
            async with asyncio.timeout(timeout):
                hits = await active.search(arguments["query"], max_results=settings.search_max_results)
            break
        except TimeoutError:
            last_error = "search timed out"
            status = ToolCallStatus.TIMEOUT
            retryable = True
            if attempt >= attempts:
                return ToolCallRecord(
                    **identity,
                    status=status,
                    attempt=attempt,
                    retryable=retryable,
                    provider=getattr(active, "provider", "none"),
                    model=getattr(active, "model", "none"),
                    error=last_error,
                    latency_ms=_latency(started),
                )
        except SearchUnavailable as exc:
            last_error = str(exc)
            status = exc.status
            retryable = exc.retryable
            if not exc.retryable or attempt >= attempts:
                return ToolCallRecord(
                    **identity,
                    status=status,
                    attempt=attempt,
                    retryable=retryable,
                    provider=getattr(active, "provider", "none"),
                    model=getattr(active, "model", "none"),
                    error=last_error,
                    latency_ms=_latency(started),
                )
    receipts = receipts_from_hits(hits, retrieved_at=utc_now())
    source_ids = [item.source_id for item in receipts]
    source_hash = content_hash(source_ids) if source_ids else ""
    result_hash = content_hash(
        {"query": arguments["query"], "source_ids": source_ids, "hashes": [item.content_hash for item in receipts]}
    )
    accepted = bool(receipts)
    return ToolCallRecord(
        **identity,
        status=ToolCallStatus.ACCEPTED if accepted else ToolCallStatus.DEGRADED,
        attempt=max(last_attempt, 1),
        retryable=False,
        provider=getattr(active, "provider", "none"),
        model=getattr(active, "model", "none"),
        latency_ms=_latency(started),
        source_hash=source_hash if accepted else "",
        result_hash=result_hash if accepted else "",
        citations=source_ids,
        sources=receipts,
        error=None if accepted else "search returned no usable sources",
    )


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def quoted_search_payload(receipts: list[SourceReceipt]) -> dict[str, Any]:
    """Fenced untrusted data. Never concatenated into system/governance prompts as instructions."""

    return fence_untrusted_data(
        [
            {
                "source_id": item.source_id,
                "url": item.url,
                "title": item.title,
                "excerpt": item.excerpt,
                "trust_status": item.trust_status,
            }
            for item in receipts
        ]
    )
