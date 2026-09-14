"""Observe-only tool belt: ToolCallRecord, cache, name-only nx, fail-closed thin evidence."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jarvis.brain.deliberation import EvidenceKind, admit_tool, withheld_commit_reply
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.brain.tools import (
    ObserveResult,
    ObserveTool,
    ToolCallRecord,
    ToolCitation,
    ToolStatus,
    make_record,
    needs_grounding,
    needs_observe,
    observe_turn,
    reset_observe_cache,
    search_nx,
    should_fail_closed,
)
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState
from jarvis.persistence import JarvisStore


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_observe_cache()
    yield
    reset_observe_cache()


def test_trivial_messages_skip_observe() -> None:
    assert needs_observe("hello") is False
    assert needs_observe("Thanks Jarvis!") is False
    assert needs_observe("Find the Nova Cortex spec on my drive") is True


def test_grounding_is_narrower_than_any_question() -> None:
    assert needs_grounding("Do you remember?") is False
    assert needs_grounding("Find the Nova Cortex spec on my drive") is True
    assert needs_grounding("Is it true that Paris is the capital of Mars?") is True


def test_tool_records_are_evidence_never_authority_or_writes() -> None:
    record = make_record(
        ObserveTool.NX_SEARCH,
        {"query": "nova cortex", "name_only": True, "limit": 8},
        status=ToolStatus.OK,
        citations=[ToolCitation(locator="G:/docs/NOVA_CORTEX.md", snippet="Nova Cortex runtime")],
    )
    admitted = admit_tool(record)
    assert record.kind == "tool_external"
    assert record.authority is False
    assert record.writes is False
    assert admitted["kind"] == EvidenceKind.TOOL_EXTERNAL.value
    assert admitted["authority"] is False
    assert admitted["writes"] is False
    assert admitted["tag"] == "observed"
    assert record.args_hash and record.payload_hash


def test_thin_evidence_fails_closed_only_after_attempted_grounding() -> None:
    skipped = make_record(ObserveTool.NX_SEARCH, {"query": "x"}, status=ToolStatus.SKIPPED, error="no bin")
    empty = make_record(ObserveTool.NX_SEARCH, {"query": "x", "name_only": True}, status=ToolStatus.EMPTY)
    message = "Find the spec on my drive"
    assert should_fail_closed(message, [skipped]) is False
    assert should_fail_closed(message, [empty]) is True
    assert should_fail_closed("hello", [empty]) is False


@pytest.mark.asyncio
async def test_name_only_nx_runs_before_full_text(monkeypatch) -> None:
    calls: list[bool] = []

    async def fake_nx(query: str, *, name_only: bool) -> ToolCallRecord:
        calls.append(name_only)
        status = ToolStatus.EMPTY if name_only else ToolStatus.OK
        cites = [] if name_only else [ToolCitation(locator="G:/docs/NOVA_CORTEX.md", snippet="runtime")]
        return make_record(
            ObserveTool.NX_SEARCH,
            {"query": query, "name_only": name_only, "limit": 8},
            status=status,
            citations=cites,
        )

    monkeypatch.setattr(settings, "nx_search_bin", "G:/nx-search/bin/nx.js")
    monkeypatch.setattr("jarvis.brain.tools._call_nx", fake_nx)
    first = await search_nx("Nova Cortex")
    second = await search_nx("Nova Cortex")
    assert calls == [True, False]
    assert first.observed is True
    assert first.args["name_only"] is False
    assert second.cached is True
    assert "scan" not in str(first.args)


@pytest.mark.asyncio
async def test_observe_order_is_ledger_then_parallel_nx_and_web(monkeypatch) -> None:
    order: list[str] = []

    async def fake_ledger(*args, **kwargs) -> ToolCallRecord:
        order.append("ledger")
        return make_record(ObserveTool.EMR_RECALL, {"query": "q"}, status=ToolStatus.EMPTY)

    async def fake_nx(*args, **kwargs) -> ToolCallRecord:
        order.append("nx")
        return make_record(ObserveTool.NX_SEARCH, {"query": "q"}, status=ToolStatus.SKIPPED, error="off")

    async def fake_web(*args, **kwargs) -> ToolCallRecord:
        order.append("web")
        return make_record(ObserveTool.WEB_SEARCH, {"query": "q"}, status=ToolStatus.SKIPPED, error="off")

    monkeypatch.setattr("jarvis.brain.tools.recall_ledger", fake_ledger)
    monkeypatch.setattr("jarvis.brain.tools.search_nx", fake_nx)
    monkeypatch.setattr("jarvis.brain.tools.search_web", fake_web)
    result = await observe_turn("Find the spec on my drive", session_id="s1", intent="transform")
    assert order[0] == "ledger"
    assert set(order[1:]) == {"nx", "web"}
    assert [item.tool for item in result.records] == [
        ObserveTool.EMR_RECALL,
        ObserveTool.NX_SEARCH,
        ObserveTool.WEB_SEARCH,
    ]
    assert all(item.writes is False for item in result.records)


@pytest.mark.asyncio
async def test_engine_fails_closed_when_grounding_evidence_is_thin(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr(settings, "observe_tools_enabled", True)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    generate = AsyncMock(return_value=LLMResult("Invented path G:/secret.md", "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)

    async def thin_observe(message: str, **kwargs):
        empty = make_record(
            ObserveTool.NX_SEARCH,
            {"query": message, "name_only": True, "limit": 8},
            status=ToolStatus.EMPTY,
        )
        return ObserveResult(required=True, grounding_required=True, records=[empty], thin=True)

    engine = JarvisEngine(store=JarvisStore(tmp_path / "observe.sqlite3"))
    monkeypatch.setattr("jarvis.brain.engine.observe_turn", thin_observe)
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(
        ChatRequest(user_id="owner", message="Find the Nova Cortex spec on my drive", session_id=state.session_id)
    )
    generate.assert_not_awaited()
    assert response.decision == "fail_closed"
    assert "don't know" in response.reply.lower()
    assert response.deliberation["committed"] is True
    assert any(item.get("kind") == EvidenceKind.TOOL_EXTERNAL.value for item in response.claims)
    assert all(item.get("authority") is False for item in response.claims)
    assert withheld_commit_reply() == response.reply


@pytest.mark.asyncio
async def test_engine_injects_nx_citations_before_commit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    generate = AsyncMock(return_value=LLMResult("The spec is at G:/docs/NOVA_CORTEX.md.", "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    hit = make_record(
        ObserveTool.NX_SEARCH,
        {"query": "Nova Cortex", "name_only": True, "limit": 8},
        status=ToolStatus.OK,
        citations=[ToolCitation(locator="G:/docs/NOVA_CORTEX.md", snippet="Nova Cortex runtime")],
    )

    async def rich_observe(message: str, **kwargs):
        return ObserveResult(required=True, grounding_required=True, records=[hit], thin=False)

    engine = JarvisEngine(store=JarvisStore(tmp_path / "observe-hit.sqlite3"))
    monkeypatch.setattr("jarvis.brain.engine.observe_turn", rich_observe)
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(
        ChatRequest(user_id="owner", message="Find Nova Cortex on my drive", session_id=state.session_id)
    )
    generate.assert_awaited()
    messages = generate.call_args.args[0]
    assert any("G:/docs/NOVA_CORTEX.md" in str(item.get("content")) for item in messages)
    assert response.decision == "answer"
    assert any(item.get("source") == "nx_search" and item["tag"] == "observed" for item in response.claims)
    assert response.deliberation["tool_records"][0]["writes"] is False
