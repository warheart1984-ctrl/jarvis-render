"""Shared v0 tool-call envelope.

Working implementation is observe-only ``web_search``. Calculator, clock,
weather, document retrieval, and health are named stubs so later tools share
this record instead of inventing a second kernel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.governance.secrets import omit_secrets

_SUMMARY_LIMIT = 240
_URL_LIMIT = 500
_QUERY_LIMIT = 500


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
    """One tool invocation. Observe-only search never writes memory or authority."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus
    transaction_id: str
    correlation_id: str
    provider: str = "none"
    model: str = "none"
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    cost_reported: bool = False
    source_hash: str = ""
    result_hash: str = ""
    citations: list[str] = Field(default_factory=list)
    sources: list[SourceReceipt] = Field(default_factory=list)
    observe_only: bool = True
    memory_written: bool = False
    authority: bool = False
    error: str | None = None
    query: str = Field(default="", max_length=_QUERY_LIMIT)

    @field_validator("authority")
    @classmethod
    def _external_never_authority(cls, value: bool) -> bool:
        return False

    @field_validator("memory_written")
    @classmethod
    def _never_auto_memory(cls, value: bool) -> bool:
        return False

    @field_validator("observe_only")
    @classmethod
    def _observe_only(cls, value: bool) -> bool:
        return True

    def to_public_dict(self) -> dict[str, Any]:
        return omit_secrets(self.model_dump(mode="json"))


STUB_TOOLS = frozenset(
    {
        ToolName.CALCULATOR,
        ToolName.CLOCK,
        ToolName.WEATHER,
        ToolName.DOCUMENT_RETRIEVAL,
        ToolName.HEALTH,
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stub_tool_call(
    name: ToolName,
    *,
    transaction_id: str,
    correlation_id: str,
    arguments: dict[str, Any] | None = None,
) -> ToolCallRecord:
    match name:
        case ToolName.WEB_SEARCH:
            raise ValueError("web_search is implemented; do not stub it")
        case (
            ToolName.CALCULATOR
            | ToolName.CLOCK
            | ToolName.WEATHER
            | ToolName.DOCUMENT_RETRIEVAL
            | ToolName.HEALTH
        ):
            pass
        case _:
            assert_never(name)
    return ToolCallRecord(
        tool_name=name.value,
        arguments=arguments or {},
        status=ToolCallStatus.NOT_IMPLEMENTED,
        transaction_id=transaction_id,
        correlation_id=correlation_id,
        provider="none",
        model="none",
        error=f"{name.value} is a named stub; only observe-only web_search is implemented",
    )
