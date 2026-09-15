"""v0 tool kit. Observe-only web search plus local calculator and clock."""

from jarvis.brain.tools.calculator import (
    evidence_from_calculator,
    maybe_calculator,
    resolve_calculator_expression,
)
from jarvis.brain.tools.clock import evidence_from_clock, maybe_clock, resolve_clock_request
from jarvis.brain.tools.dispatch import citations_from_tool_record, evidence_from_tool_record, invoke_tool
from jarvis.brain.tools.envelope import (
    STUB_TOOLS,
    SourceReceipt,
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    fence_local_data,
    fence_untrusted_data,
    may_admit_retrieved_to_memory,
    quoted_local_payload,
    stub_tool_call,
    validate_tool_arguments,
)
from jarvis.brain.tools.ledger_recall import (
    FakeLedgerRecallBackend,
    LedgerRecallOutcome,
    LedgerRecallUnavailable,
    evidence_from_recall_item,
    maybe_ledger_recall,
    quoted_recall_payload,
    recall_citation,
    resolve_recall_query,
    run_ledger_recall,
)
from jarvis.brain.tools.search import (
    FakeSearchBackend,
    evidence_from_search_hit,
    maybe_web_search,
    quoted_search_payload,
    resolve_search_query,
    search_citation,
)

__all__ = [
    "FakeLedgerRecallBackend",
    "FakeSearchBackend",
    "LedgerRecallOutcome",
    "LedgerRecallUnavailable",
    "STUB_TOOLS",
    "SourceReceipt",
    "ToolCallRecord",
    "ToolCallStatus",
    "ToolName",
    "citations_from_tool_record",
    "evidence_from_calculator",
    "evidence_from_clock",
    "evidence_from_search_hit",
    "evidence_from_tool_record",
    "fence_local_data",
    "fence_untrusted_data",
    "invoke_tool",
    "maybe_ledger_recall",
    "maybe_calculator",
    "maybe_clock",
    "maybe_web_search",
    "may_admit_retrieved_to_memory",
    "quoted_local_payload",
    "quoted_search_payload",
    "resolve_calculator_expression",
    "resolve_clock_request",
    "resolve_search_query",
    "run_ledger_recall",
    "search_citation",
    "stub_tool_call",
    "validate_tool_arguments",
]
