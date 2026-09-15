"""Continuity Ledger reconcile binds a verified ledger ID and audits it; supersede retries stay idempotent."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.continuity import ContinuityLedgerClient, WriteStatus
from jarvis.models.jarvis_types import JarvisMemoryEntry
from jarvis.persistence import JarvisStore


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.asyncio
async def test_reconcile_binds_ledger_id_persists_and_audits(tmp_path):
    engine = JarvisEngine(store=JarvisStore(tmp_path / "reconcile.sqlite3"))
    state = await engine.get_or_create_session("owner")
    memory = JarvisMemoryEntry(
        memory_id="mem-1",
        user_id="owner",
        session_id=state.session_id,
        content="durable fact",
    )
    state.long_term_memory.append(memory)
    engine._save_session(state)

    result = engine.reconcile_proposed_memory(
        state,
        memory_id="mem-1",
        ledger_memory_id="ledger-99",
        content_sha256=_sha("durable fact"),
        transaction_id="tx1",
        correlation_id="corr1",
    )
    assert result["status"] == "durably_stored"
    assert result["ledger_memory_id"] == "ledger-99"
    refreshed = engine.get_session(state.session_id)
    assert refreshed.long_term_memory[0].metadata["continuity_ledger_id"] == "ledger-99"
    assert refreshed.long_term_memory[0].metadata["reconciled"] is True
    events = [e for e in engine.get_audit(state.session_id) if e["event_type"] == "memory_reconciled"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_supersede_retries_reuse_idempotency_key(monkeypatch):
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) < 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "memory": {"id": "ledger-new", "content_sha256": "b" * 64},
                "lineage": {"supersedes": "ledger-old"},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    client = ContinuityLedgerClient("http://ledger")
    result = await client.supersede("ledger-old", "replacement text", "session-1")
    assert result["status"] == WriteStatus.ACCEPTED.value
    assert result["accepted"] is True
    assert seen[0] == seen[1]
    assert seen[0].startswith("jarvis-supersede-ledger-old-")


@pytest.mark.asyncio
async def test_propose_then_reconcile_round_trip_via_engine(tmp_path):
    engine = JarvisEngine(store=JarvisStore(tmp_path / "round.sqlite3"))
    content = "remember this"
    digest = _sha(content)
    engine.continuity = AsyncMock()
    engine.continuity.propose_memory = AsyncMock(
        return_value={
            "status": "accepted",
            "accepted": True,
            "memory": {"id": "ledger-1", "content_sha256": digest},
            "transaction_id": "t",
            "correlation_id": "c",
        }
    )
    engine.continuity.retrieve = AsyncMock(
        return_value={"memory": {"id": "ledger-1", "content_sha256": digest}}
    )
    state = await engine.get_or_create_session("owner")
    memory = JarvisMemoryEntry(
        memory_id="local-1",
        user_id="owner",
        session_id=state.session_id,
        content=content,
    )
    state.long_term_memory.append(memory)
    engine._save_session(state)

    proposed = await engine.continuity.propose_memory(
        {"content": memory.content, "session_id": state.session_id, "user_requested": True},
        f"jarvis-memory-{memory.memory_id}",
    )
    verified = await engine.continuity.retrieve(proposed["memory"]["id"])
    assert verified["memory"]["id"] == proposed["memory"]["id"]
    bound = engine.reconcile_proposed_memory(
        state,
        memory_id=memory.memory_id,
        ledger_memory_id=proposed["memory"]["id"],
        content_sha256=proposed["memory"]["content_sha256"],
        transaction_id=proposed.get("transaction_id"),
        correlation_id=proposed.get("correlation_id"),
    )
    assert bound["status"] == "durably_stored"
    assert state.long_term_memory[0].metadata["continuity_ledger_id"] == "ledger-1"
