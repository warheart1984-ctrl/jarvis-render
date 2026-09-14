"""Shared v0 tool-call envelope.

Every tool — observe-only ``web_search``, local ``calculator`` / ``clock``, and
the named stubs — uses this record. Missing identity fields fail closed.
Retrieved snippets are fenced as untrusted data, never instructions. Local
calculator and clock results are deterministic facts, still never memory.
Weather, document retrieval, and health remain stubs so later tools do not
invent a second kernel.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.governance.secrets import omit_secrets

_SUMMARY_LIMIT = 240
_URL_LIMIT = 500
_QUERY_LIMIT = 500
MAX_EXPRESSION_CHARS = 120
LOCAL_TIMEOUT_SECONDS = 1.0
UNTRUSTED_DATA_CHANNEL = "untrusted_external_data"
LOCAL_DATA_CHANNEL = "local_deterministic_data"
WHO_MAY_PROMOTE_LATER = ("user_explicit_request", "emr_gate")
_MATH_CHARS = re.compile(r"^[\d\s+\-*/().%eE]+$")


class ToolName(str, Enum):
    WEB_SEARCH = "web_search"
    CALCULATOR = "calculator"
    CLOCK = "clock"
    WEATHER = "weather"
    DOCUMENT_RETRIEVAL = "document_retrieval"
    HEALTH = "health"


class ToolCallStatus(str, Enum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    NOT_IMPLEMENTED = "not_implemented"


class SourceReceipt(BaseModel):
    """ID/receipt-safe source. Bounded excerpt; never a full scraped page."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    url: str = Field(..., max_length=_URL_LIMIT)
    retrieved_at: str
    content_hash: str
    excerpt: str = Field(..., max_length=_SUMMARY_LIMIT)
    trust_status: Literal["untrusted_external"] = "untrusted_external"
    title: str = Field(default="", max_length=120)

    @field_validator("excerpt", "title", "url")
    @classmethod
    def _clip(cls, value: str) -> str:
        return value.strip()


class ToolCallRecord(BaseModel):
    """One tool invocation. Shared by every tool. Incomplete records fail closed."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1, max_length=80)
    arguments: dict[str, Any]
    status: ToolCallStatus
    transaction_id: str = Field(..., min_length=1, max_length=128)
    correlation_id: str = Field(..., min_length=1, max_length=128)
    timeout_seconds: float = Field(..., ge=0, le=60)
    attempts: int = Field(..., ge=1, le=4)
    attempt: int = Field(..., ge=0, le=4)
    retryable: bool = False
    provider: str = "none"
    model: str = "none"
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    cost_reported: bool = False
    source_hash: str = ""
    result_hash: str = ""
    citations: list[str] = Field(default_factory=list)
    sources: list[SourceReceipt] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    observe_only: bool = True
    memory_written: bool = False
    memory_eligible: bool = False
    authority: bool = False
    error: str | None = None
    promotion: Literal["not_shipped"] = "not_shipped"

    @field_validator("authority")
    @classmethod
    def _external_never_authority(cls, value: bool) -> bool:
        return False

    @field_validator("memory_written")
    @classmethod
    def _never_auto_memory(cls, value: bool) -> bool:
        return False

    @field_validator("memory_eligible")
    @classmethod
    def _never_memory_eligible(cls, value: bool) -> bool:
        return False

    @field_validator("observe_only")
    @classmethod
    def _observe_only(cls, value: bool) -> bool:
        return True

    @model_validator(mode="after")
    def _fail_closed_incomplete(self) -> ToolCallRecord:
        if not self.tool_name.strip():
            raise ValueError("tool call missing tool_name")
        if not self.transaction_id.strip() or not self.correlation_id.strip():
            raise ValueError("tool call missing transaction_id or correlation_id")
        if self.status is ToolCallStatus.ACCEPTED:
            if not self.arguments:
                raise ValueError("accepted tool call missing validated arguments")
            if not self.source_hash or not self.result_hash:
                raise ValueError("accepted tool call missing source_hash or result_hash")
            if self.attempt < 1:
                raise ValueError("accepted tool call missing attempt metadata")
        return self

    def to_public_dict(self) -> dict[str, Any]:
        return omit_secrets(self.model_dump(mode="json"))


STUB_TOOLS = frozenset(
    {
        ToolName.WEATHER,
        ToolName.DOCUMENT_RETRIEVAL,
        ToolName.HEALTH,
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def may_admit_retrieved_to_memory(*, user_requested: bool = False) -> bool:
    """Whether retrieved tool text may become memory.

    Always false in this release, including local calculator/clock output and
    retrieved search snippets. A later path (not shipped) would require both
    an explicit user request and an EMR gate before a cited hit could become a
    draft memory or Continuity ledger record. EMR is not implemented.
    ``JARVIS_GOVERNED_WRITES_ENABLED`` stays off. ``user_requested`` cannot
    bypass that. Local deterministic facts are not a second memory path.
    """

    return False


def fence_untrusted_data(items: list[dict[str, str]]) -> dict[str, Any]:
    """Snippets are data on an untrusted channel. They are never instructions."""

    return {
        "channel": UNTRUSTED_DATA_CHANNEL,
        "kind": "data",
        "instructions": False,
        "executable": False,
        "authority": False,
        "memory_eligible": False,
        "items": items,
    }


def fence_local_data(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Local calculator/clock facts. Not web snippets; still never instructions or memory."""

    return {
        "channel": LOCAL_DATA_CHANNEL,
        "kind": "data",
        "origin": "local_deterministic",
        "instructions": False,
        "executable": False,
        "authority": False,
        "memory_eligible": False,
        "items": items,
    }


def quoted_local_payload(records: list[ToolCallRecord]) -> dict[str, Any] | None:
    """Local facts for the user channel. Never concatenated into system/governance prompts."""

    items: list[dict[str, Any]] = []
    for record in records:
        if record.tool_name not in {ToolName.CALCULATOR.value, ToolName.CLOCK.value}:
            continue
        if record.status is not ToolCallStatus.ACCEPTED:
            continue
        items.append(
            {
                "tool_name": record.tool_name,
                "arguments": record.arguments,
                "result": record.result,
                "citation_id": record.citations[0] if record.citations else "",
            }
        )
    if not items:
        return None
    return fence_local_data(items)


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Fail closed when the tool name or required args are missing/invalid."""

    name = (tool_name or "").strip()
    if not name:
        raise ValueError("tool name is required")
    try:
        tool = ToolName(name)
    except ValueError as exc:
        raise ValueError(f"unknown tool {name}") from exc
    args = dict(arguments or {})
    match tool:
        case ToolName.WEB_SEARCH:
            query = str(args.get("query") or "").strip()
            if not query or len(query) > _QUERY_LIMIT:
                raise ValueError("web_search requires a query of 1..500 characters")
            return {"query": query[:_QUERY_LIMIT]}
        case ToolName.CALCULATOR:
            expression = str(args.get("expression") or "").strip()
            if not expression or len(expression) > MAX_EXPRESSION_CHARS:
                raise ValueError(f"calculator requires an expression of 1..{MAX_EXPRESSION_CHARS} characters")
            if _MATH_CHARS.fullmatch(expression) is None:
                raise ValueError("calculator expression contains unsupported characters")
            return {"expression": expression}
        case ToolName.CLOCK:
            zone = str(args.get("timezone") or "UTC").strip() or "UTC"
            if zone.upper() != "UTC":
                raise ValueError("clock supports UTC only")
            return {"timezone": "UTC"}
        case ToolName.WEATHER | ToolName.DOCUMENT_RETRIEVAL | ToolName.HEALTH:
            return args
        case _:
            assert_never(tool)


def stub_tool_call(
    name: ToolName,
    *,
    transaction_id: str,
    correlation_id: str,
    arguments: dict[str, Any] | None = None,
    timeout_seconds: float = 0,
    attempts: int = 1,
) -> ToolCallRecord:
    match name:
        case ToolName.WEB_SEARCH | ToolName.CALCULATOR | ToolName.CLOCK:
            raise ValueError(f"{name.value} is implemented; do not stub it")
        case ToolName.WEATHER | ToolName.DOCUMENT_RETRIEVAL | ToolName.HEALTH:
            pass
        case _:
            assert_never(name)
    return ToolCallRecord(
        tool_name=name.value,
        arguments=validate_tool_arguments(name.value, arguments or {}),
        status=ToolCallStatus.NOT_IMPLEMENTED,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
        timeout_seconds=timeout_seconds,
        attempts=attempts,
        attempt=0,
        retryable=False,
        provider="none",
        model="none",
        error=(
            f"{name.value} is a named stub; implemented tools are observe-only web_search, "
            "local calculator, and local clock"
        ),
    )
