"""Shared tool dispatcher. Every kit tool returns the same ``ToolCallRecord``."""

from __future__ import annotations

from typing import Any, assert_never

from jarvis.brain.deliberation import EvidenceRef
from jarvis.brain.tools.calculator import calculator_citation, evidence_from_calculator, run_calculator
from jarvis.brain.tools.clock import clock_citation, evidence_from_clock, run_clock
from jarvis.brain.tools.envelope import (
    LOCAL_TIMEOUT_SECONDS,
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    stub_tool_call,
    validate_tool_arguments,
)
from jarvis.brain.tools.search import evidence_from_search_hit, run_web_search, search_citation
from jarvis.core.config import settings


async def invoke_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    transaction_id: str,
    correlation_id: str,
) -> ToolCallRecord:
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
            timeout_seconds=LOCAL_TIMEOUT_SECONDS,
            attempts=1,
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
        case ToolName.CALCULATOR:
            return await run_calculator(
                expression=str(validated.get("expression") or ""),
                transaction_id=transaction_id,
                correlation_id=correlation_id,
            )
        case ToolName.CLOCK:
            return await run_clock(transaction_id=transaction_id, correlation_id=correlation_id)
        case ToolName.WEATHER | ToolName.DOCUMENT_RETRIEVAL | ToolName.HEALTH:
            return stub_tool_call(
                tool,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                arguments=validated,
                timeout_seconds=settings.search_timeout_seconds,
                attempts=settings.search_attempts,
            )
        case _:
            assert_never(tool)


def evidence_from_tool_record(record: ToolCallRecord) -> list[EvidenceRef]:
    """Admit kit output as TOOL_EXTERNAL evidence. Never authority."""

    if record.tool_name == ToolName.WEB_SEARCH.value:
        return [evidence_from_search_hit(hit) for hit in record.sources]
    if record.tool_name == ToolName.CALCULATOR.value:
        item = evidence_from_calculator(record)
        return [item] if item else []
    if record.tool_name == ToolName.CLOCK.value:
        item = evidence_from_clock(record)
        return [item] if item else []
    return []


def citations_from_tool_record(record: ToolCallRecord, *, session_id: str) -> list[dict[str, Any]]:
    if record.tool_name == ToolName.WEB_SEARCH.value:
        return [search_citation(item, session_id=session_id) for item in record.sources]
    if record.tool_name == ToolName.CALCULATOR.value and record.status is ToolCallStatus.ACCEPTED:
        return [calculator_citation(record, session_id=session_id)]
    if record.tool_name == ToolName.CLOCK.value and record.status is ToolCallStatus.ACCEPTED:
        return [clock_citation(record, session_id=session_id)]
    return []
