"""Local calculator and clock: deterministic facts, never memory, never network."""

from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from jarvis.brain.context import build_chat_context
from jarvis.brain.deliberation import EvidenceKind
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.brain.tools import (
    ToolCallRecord,
    ToolCallStatus,
    ToolName,
    invoke_tool,
    may_admit_retrieved_to_memory,
    resolve_calculator_expression,
    resolve_clock_request,
)
from jarvis.brain.tools.calculator import evaluate_expression
from jarvis.brain.tools.clock import run_clock
from jarvis.core.config import settings
from jarvis.governance.hashing import content_hash
from jarvis.models.jarvis_types import ChatRequest, JarvisState
from jarvis.persistence import JarvisStore


def _engine(tmp_path, name: str = "calc") -> JarvisEngine:
    return JarvisEngine(store=JarvisStore(tmp_path / f"{name}.sqlite3"))


async def _chat(
    engine: JarvisEngine,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    *,
    reply: str = "Got it.",
    memory_consent: bool = False,
    user_id: str = "owner",
):
    mock = AsyncMock(return_value=LLMResult(reply, "test", "text-model", 12.5))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", mock)
    state = await engine.get_or_create_session(user_id)
    state.confidence = 0.84
    response = await engine.chat(
        ChatRequest(
            user_id=user_id,
            session_id=state.session_id,
            message=message,
            memory_consent=memory_consent,
        )
    )
    return response, mock


def test_resolve_calculator_requires_explicit_request() -> None:
    assert resolve_calculator_expression("Hello, 2+2 is a metaphor.") is None
    assert resolve_calculator_expression("What is the capital of France?") is None
    assert resolve_calculator_expression("2+2") is None
    assert resolve_calculator_expression("calculate 2+2") == "2+2"
    assert resolve_calculator_expression("What is 2+2?") == "2+2"


def test_resolve_clock_requires_explicit_request() -> None:
    assert resolve_clock_request("Hello Jarvis") is False
    assert resolve_clock_request("what is the weather in Paris") is False
    assert resolve_clock_request("time to build") is False
    assert resolve_clock_request("what time is it") is True
    assert resolve_clock_request("What's the current UTC time?") is True


def test_evaluate_expression_two_plus_two() -> None:
    assert evaluate_expression("2+2") == 4


def test_evaluate_expression_rejects_python_eval_traps() -> None:
    with pytest.raises(Exception):
        evaluate_expression("__import__('os').system('pwd')")
    with pytest.raises(Exception):
        evaluate_expression("2**1000")


@pytest.mark.asyncio
async def test_calculator_two_plus_two_record_fields() -> None:
    record = await invoke_tool("calculator", {"expression": "2+2"}, transaction_id="tx", correlation_id="cx")
    assert record.tool_name == ToolName.CALCULATOR.value
    assert record.status is ToolCallStatus.ACCEPTED
    assert record.arguments == {"expression": "2+2"}
    assert record.result["value"] == 4
    assert record.transaction_id == "tx"
    assert record.correlation_id == "cx"
    assert record.timeout_seconds > 0
    assert record.attempts >= 1
    assert record.attempt >= 1
    assert record.source_hash
    assert record.result_hash
    assert record.citations
    assert record.observe_only is True
    assert record.memory_written is False
    assert record.memory_eligible is False
    assert record.authority is False
    assert record.promotion == "not_shipped"
    assert record.source_hash == content_hash({"expression": "2+2"})
    assert record.result_hash == content_hash({"value": 4, "expression": "2+2"})


@pytest.mark.asyncio
async def test_calculator_invalid_expression_fail_closed() -> None:
    record = await invoke_tool(
        "calculator",
        {"expression": "not math"},
        transaction_id="tx",
        correlation_id="cx",
    )
    assert record.status is ToolCallStatus.INVALID
    assert record.memory_written is False
    assert record.result == {}
    empty = await invoke_tool("calculator", {"expression": ""}, transaction_id="tx", correlation_id="cx")
    assert empty.status is ToolCallStatus.INVALID


@pytest.mark.asyncio
async def test_calculator_does_not_run_on_every_turn(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, "no-auto")
    response, _ = await _chat(engine, monkeypatch, "Hello Jarvis, 2+2 is just a phrase.")
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_calculator_chat_is_tool_external_never_memory(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, "mem")
    response, _ = await _chat(
        engine,
        monkeypatch,
        "please calculate 2+2 and remember this preference always for later",
        reply="2+2 = 4",
        memory_consent=True,
    )
    calc = [item for item in response.tool_calls if item["tool_name"] == "calculator"]
    assert calc
    record = calc[0]
    assert record["result"]["value"] == 4
    assert record["memory_written"] is False
    assert record["memory_eligible"] is False
    assert record["observe_only"] is True
    external = [item for item in response.deliberation["evidence"] if item["kind"] == EvidenceKind.TOOL_EXTERNAL.value]
    assert any(item.get("source") == "calculator" for item in external)
    assert all(item["authority"] is False for item in external)
    assert may_admit_retrieved_to_memory(user_requested=True) is False
    assert settings.governed_writes_enabled is False
    state = engine.get_session(response.session_id)
    assert state is not None
    blob = json.dumps([entry.content for entry in state.long_term_memory])
    assert "calc-" not in blob
    required = (
        "tool_name",
        "arguments",
        "transaction_id",
        "correlation_id",
        "timeout_seconds",
        "source_hash",
        "result_hash",
        "attempt",
    )
    for field in required:
        assert record[field]


@pytest.mark.asyncio
async def test_clock_returns_utc_timestamp_without_network(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("clock must not use the network")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    record = await run_clock(transaction_id="tx", correlation_id="cx")
    assert record.tool_name == ToolName.CLOCK.value
    assert record.status is ToolCallStatus.ACCEPTED
    assert record.result["timezone"] == "UTC"
    assert record.result["iso8601"]
    assert "T" in record.result["iso8601"]
    assert record.result["unix_seconds"] > 0
    assert record.memory_written is False
    assert record.memory_eligible is False
    assert record.authority is False
    assert record.source_hash
    assert record.result_hash
    assert record.transaction_id == "tx"
    assert record.correlation_id == "cx"


@pytest.mark.asyncio
async def test_clock_chat_no_memory_write(tmp_path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("clock must not use the network")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    engine = _engine(tmp_path, "clock")
    response, _ = await _chat(
        engine,
        monkeypatch,
        "what time is it right now please remember this always",
        reply="The UTC clock fact is noted.",
        memory_consent=True,
    )
    clock = [item for item in response.tool_calls if item["tool_name"] == "clock"]
    assert clock
    iso = clock[0]["result"]["iso8601"]
    assert iso
    assert clock[0]["memory_written"] is False
    assert may_admit_retrieved_to_memory(user_requested=True) is False
    state = engine.get_session(response.session_id)
    assert state is not None
    assert all(iso not in entry.content for entry in state.long_term_memory)
    external = [item for item in response.deliberation["evidence"] if item.get("source") == "clock"]
    assert external
    assert all(item["authority"] is False for item in external)
    assert all(item["kind"] == EvidenceKind.TOOL_EXTERNAL.value for item in external)


def test_local_facts_stay_off_the_system_prompt() -> None:
    state = JarvisState(user_id="u", session_id="s")
    messages, facts = build_chat_context(
        state,
        ChatRequest(user_id="u", message="calculate 2+2"),
        read_only=False,
        infinity_configured=False,
        continuity_configured=False,
        speech_configured=False,
        local_tool_quotes={
            "channel": "local_deterministic_data",
            "kind": "data",
            "instructions": False,
            "executable": False,
            "authority": False,
            "memory_eligible": False,
            "items": [{"tool_name": "calculator", "result": {"value": 4, "expression": "2+2"}}],
        },
        calculator_status="accepted",
    )
    system = messages[0]["content"]
    assert "Local deterministic tool facts" not in system
    assert '"value": 4' not in system
    quoted = [m["content"] for m in messages if "Local deterministic tool facts" in m["content"]]
    assert quoted
    assert "not memory" in quoted[0]
    assert facts["local_tools_observe_only"] is True
    assert facts["local_tool_facts_in_context"] == 1


def test_missing_tool_call_fields_fail_closed_for_calculator() -> None:
    with pytest.raises(ValidationError):
        ToolCallRecord(
            tool_name="calculator",
            arguments={"expression": "2+2"},
            status=ToolCallStatus.ACCEPTED,
            timeout_seconds=1,
            attempts=1,
            attempt=1,
        )
    with pytest.raises(ValidationError):
        ToolCallRecord(
            tool_name="calculator",
            arguments={"expression": "2+2"},
            status=ToolCallStatus.ACCEPTED,
            transaction_id="t",
            correlation_id="c",
            timeout_seconds=1,
            attempts=1,
            attempt=1,
            source_hash="",
            result_hash="",
        )
