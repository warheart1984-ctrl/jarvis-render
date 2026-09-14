"""Observe-only tool belt: ledger recall, then parallel nx_search + web search.

Results are ToolCallRecord evidence (never authority, never memory writes).
Jarvis must not walk drives; nx_search/nx_stats are the only local corpus path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.core.config import settings
from jarvis.governance.hashing import content_hash
from jarvis.governance.secrets import omit_secrets

QUERY_LIMIT = 200
SNIPPET_LIMIT = 280
MAX_CITATIONS = 8
NX_NAME_LIMIT = 8
RETRY_ATTEMPTS = 2

_TRIVIAL = re.compile(
    r"^(hi|hello|hey|thanks|thank you|ok|okay|yo|good morning|good night)"
    r"([\s,!.]*jarvis)?[\s!.?]*$",
    re.IGNORECASE,
)
_FILE_GROUNDING = re.compile(
    r"\b(find|locate|filename|filepath|nx search|on (my )?(disk|drive)|indexed|"
    r"where (is|are) (the |that )?(file|spec|doc|pdf|code)|vault)\b",
    re.IGNORECASE,
)
_WEB_GROUNDING = re.compile(
    r"\b(search the web|look (this|that|it) up|latest news|according to (the )?(internet|web)|cite (a |the )?source)\b",
    re.IGNORECASE,
)
_FACT_GROUNDING = re.compile(
    r"\b(fact[- ]check|is it true( that)?|prove that|verify that)\b",
    re.IGNORECASE,
)
_DDG_LINK = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class ObserveTool(str, Enum):
    EMR_RECALL = "emr_recall"
    NX_SEARCH = "nx_search"
    WEB_SEARCH = "web_search"


class ToolStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"


class ToolCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: str = Field(..., max_length=1024)
    snippet: str = Field(default="", max_length=SNIPPET_LIMIT)
    title: str = Field(default="", max_length=200)
    content_sha256: str = Field(default="", max_length=64)

    @field_validator("snippet", "title", "locator")
    @classmethod
    def _clip(cls, value: str, info: Any) -> str:
        limits = {"locator": 1024, "title": 200, "snippet": SNIPPET_LIMIT}
        return " ".join(value.split())[: limits.get(info.field_name or "snippet", SNIPPET_LIMIT)]


class ToolCallRecord(BaseModel):
    """One observe-only tool invocation. Writes are forbidden."""

    model_config = ConfigDict(extra="forbid")

    tool: ObserveTool
    args: dict[str, Any] = Field(default_factory=dict)
    args_hash: str
    status: ToolStatus
    kind: Literal["tool_external"] = "tool_external"
    authority: Literal[False] = False
    writes: Literal[False] = False
    observed: bool = False
    citations: list[ToolCitation] = Field(default_factory=list)
    payload_hash: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    error: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return omit_secrets(self.model_dump(mode="json"))


class ObserveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool
    grounding_required: bool
    records: list[ToolCallRecord] = Field(default_factory=list)
    thin: bool = False

    def observed_citations(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for record in self.records:
            if not record.observed:
                continue
            for citation in record.citations:
                items.append(
                    {
                        "tool": record.tool.value,
                        "locator": citation.locator,
                        "snippet": citation.snippet,
                        "title": citation.title,
                        "content_sha256": citation.content_sha256,
                        "args_hash": record.args_hash,
                    }
                )
        return items[:MAX_CITATIONS]

    def public_dict(self) -> dict[str, Any]:
        return omit_secrets(self.model_dump(mode="json"))


class ToolResultCache:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else settings.observe_cache_ttl_seconds
        self._items: dict[str, tuple[float, ToolCallRecord]] = {}

    def key(self, tool: ObserveTool, args_hash: str) -> str:
        return f"{tool.value}:{args_hash}"

    def get(self, tool: ObserveTool, args_hash: str) -> ToolCallRecord | None:
        item = self._items.get(self.key(tool, args_hash))
        if item is None:
            return None
        expires, record = item
        if expires < time.monotonic():
            self._items.pop(self.key(tool, args_hash), None)
            return None
        return record.model_copy(update={"cached": True})

    def put(self, record: ToolCallRecord) -> ToolCallRecord:
        if record.status in {ToolStatus.OK, ToolStatus.EMPTY}:
            self._items[self.key(record.tool, record.args_hash)] = (
                time.monotonic() + self.ttl_seconds,
                record.model_copy(update={"cached": False}),
            )
        return record

    def clear(self) -> None:
        self._items.clear()


_CACHE = ToolResultCache()


def reset_observe_cache() -> None:
    _CACHE.clear()


def clip_query(message: str, limit: int = QUERY_LIMIT) -> str:
    cleaned = " ".join((message or "").split()).strip()
    if cleaned.startswith("-"):
        cleaned = cleaned.lstrip("-")
    return cleaned[:limit]


def needs_observe(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    return _TRIVIAL.fullmatch(text) is None


def needs_grounding(message: str) -> bool:
    text = message or ""
    return bool(_FILE_GROUNDING.search(text) or _WEB_GROUNDING.search(text) or _FACT_GROUNDING.search(text))


def should_fail_closed(message: str, records: list[ToolCallRecord]) -> bool:
    if not needs_grounding(message):
        return False
    attempted = [item for item in records if item.status is not ToolStatus.SKIPPED]
    if not attempted:
        return False
    return not any(item.observed and item.citations for item in attempted)


def make_record(
    tool: ObserveTool,
    args: dict[str, Any],
    *,
    status: ToolStatus,
    citations: list[ToolCitation] | None = None,
    error: str | None = None,
    latency_ms: float = 0.0,
    cached: bool = False,
) -> ToolCallRecord:
    cites = (citations or [])[:MAX_CITATIONS]
    digest = content_hash({"tool": tool.value, "args": args})
    payload = content_hash(
        [{"locator": item.locator, "snippet": item.snippet, "title": item.title} for item in cites]
    )
    observed = status is ToolStatus.OK and bool(cites)
    return ToolCallRecord(
        tool=tool,
        args=args,
        args_hash=digest,
        status=status if cites or status is not ToolStatus.OK else ToolStatus.EMPTY,
        citations=cites,
        payload_hash=payload,
        observed=observed,
        error=error,
        latency_ms=round(latency_ms, 2),
        cached=cached,
    )


def _citation(locator: str, snippet: str = "", title: str = "") -> ToolCitation:
    text = snippet or title or locator
    return ToolCitation(
        locator=locator[:1024],
        snippet=text[:SNIPPET_LIMIT],
        title=(title or "")[:200],
        content_sha256=content_hash(text),
    )


def _skipped(tool: ObserveTool, args: dict[str, Any], reason: str) -> ToolCallRecord:
    return make_record(tool, args, status=ToolStatus.SKIPPED, error=reason)


async def observe_turn(
    message: str,
    *,
    session_id: str,
    intent: str = "transform",
    ledger: Any | None = None,
    cache: ToolResultCache | None = None,
) -> ObserveResult:
    """Step 0: emr_recall. Then parallel nx_search + web_search. Never writes."""

    cache = cache or _CACHE
    required = needs_observe(message)
    grounding = needs_grounding(message)
    if not required:
        return ObserveResult(required=False, grounding_required=False, records=[], thin=False)

    query = clip_query(message)
    ledger_record = await recall_ledger(query, intent=intent, session_id=session_id, ledger=ledger, cache=cache)
    nx_task = search_nx(query, cache=cache)
    web_task = search_web(query, cache=cache)
    nx_record, web_record = await asyncio.gather(nx_task, web_task)
    records = [ledger_record, nx_record, web_record]
    return ObserveResult(
        required=True,
        grounding_required=grounding,
        records=records,
        thin=should_fail_closed(message, records),
    )


async def recall_ledger(
    query: str,
    *,
    intent: str,
    session_id: str,
    ledger: Any | None,
    cache: ToolResultCache | None = None,
) -> ToolCallRecord:
    args = {"query": query, "intent": intent, "session_id": session_id}
    if ledger is None or not settings.continuity_ledger_url:
        return _skipped(ObserveTool.EMR_RECALL, args, "continuity ledger not configured")
    return await _cached_call(ObserveTool.EMR_RECALL, args, cache, lambda: _call_ledger(ledger, args))


async def search_nx(query: str, *, cache: ToolResultCache | None = None) -> ToolCallRecord:
    bin_path = (settings.nx_search_bin or "").strip()
    args = {"query": query, "name_only": True, "limit": settings.observe_nx_limit}
    if not bin_path:
        return _skipped(ObserveTool.NX_SEARCH, args, "nx_search bin not configured")
    named = await _cached_call(ObserveTool.NX_SEARCH, args, cache, lambda: _call_nx(query, name_only=True))
    if named.observed or named.status in {ToolStatus.TIMEOUT, ToolStatus.ERROR, ToolStatus.SKIPPED}:
        return named
    fts_args = {"query": query, "name_only": False, "limit": settings.observe_nx_limit}
    return await _cached_call(ObserveTool.NX_SEARCH, fts_args, cache, lambda: _call_nx(query, name_only=False))


async def search_web(query: str, *, cache: ToolResultCache | None = None) -> ToolCallRecord:
    args = {"query": query, "provider": settings.web_search_provider}
    provider = (settings.web_search_provider or "none").lower()
    if not settings.web_search_enabled or provider in {"", "none"}:
        return _skipped(ObserveTool.WEB_SEARCH, args, "web search not configured")
    return await _cached_call(ObserveTool.WEB_SEARCH, args, cache, lambda: _call_web(query, provider))


async def _cached_call(
    tool: ObserveTool,
    args: dict[str, Any],
    cache: ToolResultCache | None,
    factory: Any,
) -> ToolCallRecord:
    digest = content_hash({"tool": tool.value, "args": args})
    store = cache or _CACHE
    hit = store.get(tool, digest)
    if hit is not None:
        return hit
    started = time.perf_counter()
    try:
        record = await factory()
    except TimeoutError:
        record = make_record(tool, args, status=ToolStatus.TIMEOUT, error="timeout", latency_ms=_ms(started))
    except Exception as exc:
        record = make_record(tool, args, status=ToolStatus.ERROR, error=type(exc).__name__, latency_ms=_ms(started))
    if record.latency_ms == 0:
        record = record.model_copy(update={"latency_ms": round(_ms(started), 2)})
    return store.put(record)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def _call_ledger(ledger: Any, args: dict[str, Any]) -> ToolCallRecord:
    async def once() -> dict[str, Any]:
        return await ledger.recall(args["session_id"], args["query"], args["intent"])

    payload = await _retry(once)
    citations = _ledger_citations(payload)
    abstained = bool(payload.get("abstained"))
    status = ToolStatus.EMPTY if abstained or not citations else ToolStatus.OK
    return make_record(
        ObserveTool.EMR_RECALL,
        args,
        status=status,
        citations=citations,
        error="abstained" if abstained else None,
    )


def _ledger_citations(payload: dict[str, Any]) -> list[ToolCitation]:
    citations: list[ToolCitation] = []
    bundle = payload.get("bundle") or payload.get("records") or []
    if isinstance(bundle, dict):
        bundle = bundle.get("items") or bundle.get("memories") or []
    if not isinstance(bundle, list):
        return citations
    for item in bundle:
        if not isinstance(item, dict):
            continue
        locator = str(item.get("id") or item.get("memory_id") or item.get("locator") or "ledger")
        snippet = str(item.get("content") or item.get("summary") or item.get("text") or "")
        if snippet:
            citations.append(_citation(locator, snippet, str(item.get("type") or item.get("subject") or "")))
        if len(citations) >= MAX_CITATIONS:
            break
    return citations


async def _call_nx(query: str, *, name_only: bool) -> ToolCallRecord:
    args = {"query": query, "name_only": name_only, "limit": settings.observe_nx_limit}
    bin_path = settings.nx_search_bin.strip()
    command = [
        settings.nx_search_node.strip() or "node",
        bin_path,
        "search",
        "--json",
        "--limit",
        str(settings.observe_nx_limit),
    ]
    if name_only:
        command.append("--name-only")
    command.append(query)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.observe_tool_timeout_seconds)
    except TimeoutError:
        return make_record(ObserveTool.NX_SEARCH, args, status=ToolStatus.TIMEOUT, error="timeout")
    except FileNotFoundError:
        return make_record(ObserveTool.NX_SEARCH, args, status=ToolStatus.ERROR, error="node or nx bin missing")
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace")[:160] or f"exit {proc.returncode}"
        return make_record(ObserveTool.NX_SEARCH, args, status=ToolStatus.ERROR, error=detail)
    try:
        payload = json.loads((stdout or b"").decode("utf-8"))
    except json.JSONDecodeError:
        return make_record(ObserveTool.NX_SEARCH, args, status=ToolStatus.ERROR, error="invalid nx json")
    if not isinstance(payload, dict):
        return make_record(ObserveTool.NX_SEARCH, args, status=ToolStatus.ERROR, error="invalid nx payload")
    citations = _nx_citations(payload)
    status = ToolStatus.EMPTY if not citations else ToolStatus.OK
    hint = str(payload.get("hint") or "") if payload.get("emptyIndex") else None
    return make_record(ObserveTool.NX_SEARCH, args, status=status, citations=citations, error=hint)


def _nx_citations(payload: dict[str, Any]) -> list[ToolCitation]:
    citations: list[ToolCitation] = []
    for hit in payload.get("filenames") or []:
        if not isinstance(hit, dict) or not hit.get("path"):
            continue
        path = str(hit["path"])
        citations.append(_citation(path, path))
        if len(citations) >= MAX_CITATIONS:
            return citations
    for hit in payload.get("content") or []:
        if not isinstance(hit, dict) or not hit.get("path"):
            continue
        path = str(hit["path"])
        snippet = str(hit.get("snippet") or path)
        citations.append(_citation(path, snippet))
        if len(citations) >= MAX_CITATIONS:
            break
    return citations


async def _call_web(query: str, provider: str) -> ToolCallRecord:
    args = {"query": query, "provider": provider}
    match provider:
        case "brave":
            citations = await _web_brave(query)
        case "ddg":
            citations = await _web_ddg(query)
        case _:
            return _skipped(ObserveTool.WEB_SEARCH, args, f"unknown web provider {provider}")
    status = ToolStatus.EMPTY if not citations else ToolStatus.OK
    return make_record(ObserveTool.WEB_SEARCH, args, status=status, citations=citations)


async def _web_brave(query: str) -> list[ToolCitation]:
    token = settings.brave_search_api_key.strip()
    if not token:
        raise RuntimeError("brave key missing")

    async def once() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.observe_tool_timeout_seconds) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 5},
                headers={"X-Subscription-Token": token, "Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("invalid brave payload")
            return data

    payload = await _retry(once)
    citations: list[ToolCitation] = []
    for item in ((payload.get("web") or {}).get("results") or [])[:5]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        citations.append(
            _citation(str(item["url"]), str(item.get("description") or ""), str(item.get("title") or ""))
        )
    return citations


async def _web_ddg(query: str) -> list[ToolCitation]:
    async def once() -> str:
        async with httpx.AsyncClient(timeout=settings.observe_tool_timeout_seconds, follow_redirects=True) as client:
            response = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "JarvisObserve/0.1"},
            )
            response.raise_for_status()
            return response.text

    html = await _retry(once)
    snippets = [_TAG.sub(" ", item).strip() for item in _DDG_SNIPPET.findall(html)]
    citations: list[ToolCitation] = []
    for index, match in enumerate(_DDG_LINK.finditer(html)):
        href, title_html = match.group(1), match.group(2)
        locator = _ddg_url(href)
        if not locator:
            continue
        title = _TAG.sub(" ", title_html).strip()
        snippet = snippets[index] if index < len(snippets) else title
        citations.append(_citation(locator, snippet, title))
        if len(citations) >= 5:
            break
    return citations


def _ddg_url(href: str) -> str:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return unquote(query["uddg"][0])[:1024]
    if href.startswith("http"):
        return href[:1024]
    return ""


async def _retry(factory: Any) -> Any:
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await asyncio.wait_for(factory(), timeout=settings.observe_tool_timeout_seconds)
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
            last = exc
            if attempt == RETRY_ATTEMPTS - 1:
                if isinstance(exc, httpx.NetworkError):
                    raise
                raise TimeoutError(str(exc)) from exc
            await asyncio.sleep(0.05 * (2**attempt))
    raise last or RuntimeError("retry exhausted")
