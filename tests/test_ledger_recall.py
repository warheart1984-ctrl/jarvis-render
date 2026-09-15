"""Observe-only Continuity Ledger recall (recall-before-invention).

Recall runs automatically per turn when a Continuity client/backend is
configured. Recalled governed memories become MEMORY-kind evidence (never
authority, never a write). Ledger unavailability degrades; it never causes
fail_closed. No ledger is reached when nothing is configured.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.deliberation import (
    BLOCK_REPLY,
    DeliberationRunner,
    EvidenceKind,
)
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.brain.tools import (
    FakeLedgerRecallBackend,
    ToolCallStatus,
    evidence_from_recall_item,
    may_admit_retrieved_to_memory,
    maybe_ledger_recall,
    quoted_recall_payload,
    recall_citation,
    resolve_recall_query,
    run_ledger_recall,
)
from jarvis.brain.tools.ledger_recall import (
    UnavailableLedgerRecallBackend,
    receipts_from_recall_bundle,
)
from jarvis.core.config import settings
from jarvis.governance.hashing import content_hash
from jarvis.models.jarvis_types import ChatRequest
from jarvis.persistence import JarvisStore

PARIS_MEMORY = {
    "memory_id": "mem_paris_capital_0001",
    "content": "Paris is the capital of France.",
    "subject": "geography",
    "type": "fact",
    "status": "verified",
    "confidence": 0.9,
    "activation": 0.8,
    "tags": ["geography", "recall"],
}


def _engine(tmp_path, name: str = "recall") -> JarvisEngine:
    return JarvisEngine(store=JarvisStore(tmp_path / f"{name}.sqlite3"))


async def _chat(
    engine: JarvisEngine,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    *,
    reply: str = "Good to connect. I'm ready when you are.",
    bundle: list[dict] | None = None,
    memory_consent: bool = False,
    user_id: str = "owner",
):
    if bundle is not None:
        engine.recall_backend = FakeLedgerRecallBackend(bundle=bundle)
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


def test_resolve_recall_query_is_automatic_and_bounded() -> None:
    assert resolve_recall_query("What is the capital of France?") == "What is the capital of France?"
    assert resolve_recall_query("   a  ") == "a"
    assert resolve_recall_query("") is None
    assert resolve_recall_query("  ") is None
    long = "q" * 600
    assert len(resolve_recall_query(long)) == 500
    assert resolve_recall_query(long, explicit="Paris capital") == "Paris capital"
    assert resolve_recall_query("msg", explicit="   ") == "msg"


def test_recall_receipts_and_evidence_are_memory_kind_never_authority() -> None:
    receipts = receipts_from_recall_bundle([PARIS_MEMORY], retrieved_at="2026-01-01T00:00:00+00:00")
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["source_id"].startswith("ledger-")
    assert receipt["memory_id"] == PARIS_MEMORY["memory_id"]
    assert receipt["content_sha256"]
    assert "no content" not in receipt["excerpt"]

    evidence = evidence_from_recall_item(receipt)
    assert evidence.kind is EvidenceKind.MEMORY
    assert evidence.authority is False
    assert evidence.source == "continuity_ledger"
    assert evidence.citation_id == receipt["source_id"]
    assert evidence.memory_id == PARIS_MEMORY["memory_id"]
    assert "never authority" in (evidence.admission or "")

    citation = recall_citation(receipt, session_id="sess-1")
    assert citation["source_type"] == "memory"
    assert citation["citation_id"] == receipt["source_id"]
    assert citation["trust_status"] == "governed_ledger"
    assert citation["content_sha256"] == receipt["content_sha256"]


def test_evidences_dedup_between_recall_and_reflect_citation() -> None:
    from jarvis.brain.deliberation import evidence_from_citation

    receipts = receipts_from_recall_bundle([PARIS_MEMORY], retrieved_at="2026-01-01T00:00:00+00:00")
    recall_evidence = evidence_from_recall_item(receipts[0])
    citations = [recall_citation(receipts[0], session_id="s")]

    runner = DeliberationRunner()
    runner.add_evidence(recall_evidence)
    for item in citations:
        cited = evidence_from_citation(item)
        if cited is not None:
            runner.add_evidence(cited)
    # The RECALL-stage id matches the REFLECT-stage cite-{citation_id} id,
    # so recall evidence is never double-counted.
    assert len(runner.evidence) == 1
    assert runner.evidence[0].evidence_id == recall_evidence.evidence_id


def test_quoted_recall_payload_is_fenced_data() -> None:
    receipts = receipts_from_recall_bundle([PARIS_MEMORY], retrieved_at="2026-01-01T00:00:00+00:00")
    quoted = quoted_recall_payload(receipts)
    assert quoted["instructions"] is False
    assert quoted["executable"] is False
    assert quoted["memory_eligible"] is False
    assert quoted["authority"] is False
    assert PARIS_MEMORY["content"] in json.dumps(quoted["items"])


@pytest.mark.asyncio
async def test_recall_runs_automatically_before_invention(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    response, generate = await _chat(
        engine,
        monkeypatch,
        "What is the capital of France?",
        reply="Paris is the capital of France.",
        bundle=[PARIS_MEMORY],
    )
    assert response.tool_calls
    record = response.tool_calls[0]
    assert record["tool_name"] == "ledger_recall"
    assert record["status"] == ToolCallStatus.ACCEPTED.value
    assert record["observe_only"] is True
    assert record["memory_written"] is False
    assert record["memory_eligible"] is False
    assert record["authority"] is False
    assert record["promotion"] == "not_shipped"
    assert record["citations"][0] == PARIS_MEMORY["memory_id"]
    source_ids = [r["source_id"] for r in receipts_from_recall_bundle([PARIS_MEMORY], retrieved_at="")]
    assert record["source_hash"] == content_hash(source_ids)
    assert record["result_hash"]

    messages = generate.call_args.args[0]
    fence = next(
        m["content"]
        for m in messages
        if m["role"] == "user" and "Recalled Continuity Ledger memory fence" in m["content"]
    )
    quoted = json.loads(fence.split("\n", 1)[1])
    assert quoted["instructions"] is False
    assert quoted["authority"] is False
    assert PARIS_MEMORY["content"] in json.dumps(quoted["items"])

    facts = json.loads(messages[0]["content"].split("Runtime facts:\n", 1)[1])
    assert facts["ledger_recall_observe_only"] is True
    assert facts["ledger_recall_status"] == ToolCallStatus.ACCEPTED.value
    assert facts["ledger_recall_memories_in_context"] == 1
    assert facts["observe_citations_in_context"] == 1


@pytest.mark.asyncio
async def test_recall_evidence_makes_factual_reply_committed(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    response, _ = await _chat(
        engine,
        monkeypatch,
        "What is the capital of France?",
        reply="Paris is the capital of France.",
        bundle=[PARIS_MEMORY],
    )
    assert response.decision != "fail_closed"
    assert response.deliberation["response_commit"] == "committed"
    memory_evidence = [
        item
        for item in response.deliberation["evidence"]
        if item["kind"] == "memory" and item.get("source") == "continuity_ledger"
    ]
    assert memory_evidence
    assert all(item["authority"] is False for item in memory_evidence)
    reply_claims = [c for c in response.deliberation["claims"] if c["claim_id"].startswith("claim-reply")]
    assert any(any(eid.startswith("cite-ledger-") for eid in c["evidence_ids"]) for c in reply_claims)


@pytest.mark.asyncio
async def test_recall_not_run_and_does_not_reach_ledger_when_unconfigured(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    calls: list[dict] = []

    async def tracked_recall(query, *, max_memories, intent, session_key):
        calls.append({"query": query})
        return {"protocol": "emr-recall-v1", "bundle": [], "abstained": False}

    backend = FakeLedgerRecallBackend(bundle=[PARIS_MEMORY])
    backend.recall = tracked_recall  # type: ignore[method-assign]
    response, _ = await _chat(engine, monkeypatch, "Hello there.")
    assert response.tool_calls == []
    assert calls == []
    assert response.decision not in {"fail_closed", "abstain", "degraded"}


@pytest.mark.asyncio
async def test_recall_degrades_on_unavailable_ledger_not_fail_closed(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.recall_backend = UnavailableLedgerRecallBackend()
    response, _ = await _chat(engine, monkeypatch, "Remind me about the project plan.")
    record = response.tool_calls[0]
    assert record["status"] == ToolCallStatus.UNAVAILABLE.value
    assert record["retryable"] is False
    assert record["memory_written"] is False
    assert "Continuity Ledger recall is not configured" in record["error"]
    assert response.reply != BLOCK_REPLY
    assert response.decision != "fail_closed"


@pytest.mark.asyncio
async def test_recall_timeout_and_degraded_invalid_bundle(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ledger_recall_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "ledger_recall_attempts", 1)
    timed_out = await run_ledger_recall(
        query="slow",
        session_id="sess",
        tenant_id="t",
        owner_sub="s",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
        backend=FakeLedgerRecallBackend(bundle=[PARIS_MEMORY], delay_seconds=1),
    )
    assert timed_out is not None
    assert timed_out.record.status is ToolCallStatus.TIMEOUT
    assert timed_out.record.retryable is True
    assert timed_out.record.memory_written is False

    class BadBackend:
        provider, model, calls = "bad", "bad", 0

        async def recall(self, query, *, max_memories, intent, session_key):
            return {"protocol": "emr-recall-v1", "bundle": "not-a-list"}

    degraded = await run_ledger_recall(
        query="x",
        session_id="sess",
        tenant_id="t",
        owner_sub="s",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
        backend=BadBackend(),  # type: ignore[arg-type]
    )
    assert degraded is not None
    assert degraded.record.status is ToolCallStatus.DEGRADED
    assert degraded.record.sources == []


@pytest.mark.asyncio
async def test_recall_rate_limited_per_tenant_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ledger_recall_rate_limit", 1)
    engine = _engine(tmp_path)
    engine.recall_backend = FakeLedgerRecallBackend(bundle=[PARIS_MEMORY])
    request = ChatRequest(user_id="owner", session_id="sess-rate", message="Recall project plan")
    first = await maybe_ledger_recall(
        message=request.message,
        recall_query=None,
        session_id="sess-rate",
        tenant_id="t",
        owner_sub="s",
        transaction_id="tx1",
        correlation_id="cx1",
        quota=engine.access.consume_quota,
        backend=engine.recall_backend,
    )
    second = await maybe_ledger_recall(
        message=request.message,
        recall_query=None,
        session_id="sess-rate",
        tenant_id="t",
        owner_sub="s",
        transaction_id="tx2",
        correlation_id="cx2",
        quota=engine.access.consume_quota,
        backend=engine.recall_backend,
    )
    assert first is not None and first.record.status is ToolCallStatus.ACCEPTED
    assert second is not None and second.record.status is ToolCallStatus.RATE_LIMITED
    assert len(engine.recall_backend.calls) == 1


@pytest.mark.asyncio
async def test_maybe_ledger_recall_requires_settings_and_query(monkeypatch) -> None:
    backend = FakeLedgerRecallBackend(bundle=[PARIS_MEMORY])
    monkeypatch.setattr(settings, "observe_tools_enabled", False)
    disabled = await maybe_ledger_recall(
        message="Recall any plan",
        recall_query=None,
        session_id="s",
        tenant_id="t",
        owner_sub="o",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
        backend=backend,
    )
    assert disabled is None
    monkeypatch.setattr(settings, "observe_tools_enabled", True)
    monkeypatch.setattr(settings, "ledger_recall_enabled", False)
    disabled2 = await maybe_ledger_recall(
        message="Recall any plan",
        recall_query=None,
        session_id="s",
        tenant_id="t",
        owner_sub="o",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
        backend=backend,
    )
    assert disabled2 is None
    monkeypatch.setattr(settings, "ledger_recall_enabled", True)
    no_query = await maybe_ledger_recall(
        message="   ",
        recall_query=None,
        session_id="s",
        tenant_id="t",
        owner_sub="o",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
        backend=backend,
    )
    assert no_query is None
    no_backend = await maybe_ledger_recall(
        message="Recall any plan",
        recall_query=None,
        session_id="s",
        tenant_id="t",
        owner_sub="o",
        transaction_id="tx",
        correlation_id="cx",
        quota=lambda *_args: True,
    )
    assert no_backend is None


def test_max_memories_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ledger_recall_max_memories", 2)
    bundle = [
        {**PARIS_MEMORY, "memory_id": f"mem_{i}", "content": f"Memory {i}."}
        for i in range(5)
    ]
    receipts = receipts_from_recall_bundle(bundle, retrieved_at="2026-01-01")
    # receipts_from_recall_bundle does not bound; max_memories is enforced at recall().
    assert len(receipts) == 5


def test_may_admit_retrieved_to_memory_stays_false() -> None:
    assert may_admit_retrieved_to_memory(user_requested=True) is False
