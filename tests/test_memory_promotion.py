"""OTEM-lite governed memory promotion: preview never writes; apply refuses when gated.

The promotion path is proposal -> preview (read-only) -> explicit user request ->
EMR gate -> apply to Continuity. The durable write lock keeps apply disabled by
default; preview is always safe for authenticated owners.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.brain.tools.memory_promotion import (
    apply_promotion,
    build_proposal,
    check_gate,
    render_preview,
)
from jarvis.core.config import settings
from jarvis.models.jarvis_types import JarvisMemoryEntry, JarvisState
from jarvis.persistence import JarvisStore
from tests.test_chat_voice import client as client  # noqa: F401


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _engine(tmp_path, name: str = "promote") -> JarvisEngine:
    return JarvisEngine(store=JarvisStore(tmp_path / f"{name}.sqlite3"))


def _state_with_draft(engine: JarvisEngine, *, content: str, category: str = "preference") -> JarvisState:
    state = JarvisState(user_id="owner", session_id="s-promote")
    # Mimic extract_memory prefixes so promotion is allowed
    if category == "preference":
        prefixed = f"Preference: {content}"
    else:
        prefixed = f"User said: {content}"
    entry = JarvisMemoryEntry(
        memory_id="local-1",
        user_id="owner",
        session_id="s-promote",
        content=prefixed,
        category=category,
        importance=0.7,
    )
    entry.metadata["status"] = "draft"
    state.long_term_memory.append(entry)
    return state


@pytest.mark.asyncio
async def test_preview_never_calls_ledger(tmp_path):
    engine = _engine(tmp_path)
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state

    proposal = build_proposal(state, state.long_term_memory[0])
    gate = check_gate(continuity_configured=engine.continuity is not None, read_only=False)
    preview = render_preview(proposal, gate)

    assert preview["preview_only"] is True
    assert preview["no_write"] is True
    assert preview["authority"] is False
    assert preview["memory_eligible"] is False
    assert proposal.user_requested is False
    assert engine.continuity is None

    assert preview["proposal"]["ledger_type"] == "preference"
    assert preview["proposal"]["content_sha256"] == _sha("Preference: I prefer concrete builds.")
    assert preview["content_preview"] == "Preference: I prefer concrete builds."


def test_build_proposal_maps_type_and_subject() -> None:
    state = _state_with_draft(
        JarvisEngine(store=None),  # type: ignore[arg-type]
        content="I always prefer fast setups.",
    )
    proposal = build_proposal(state, state.long_term_memory[0])
    assert proposal.ledger_type == "preference"
    assert proposal.subject == "owner"
    assert proposal.idempotency_key.startswith("jarvis-promote-local-1-")
    assert proposal.evidence == [{"kind": "chat", "ref": "local-1"}]


def test_build_proposal_architecture_subject_and_research_type() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    entry = JarvisMemoryEntry(
        memory_id="local-2",
        user_id="owner",
        session_id="s",
        content="User said: Build a spiral model",
        category="spiral_intelligence",
    )
    entry.metadata["status"] = "draft"
    state.long_term_memory.append(entry)
    proposal = build_proposal(state, state.long_term_memory[0])
    assert proposal.ledger_type == "architecture"
    assert proposal.subject == "jarvis-prototype"
    assert proposal.tags == ["spiral_intelligence"]


def test_build_proposal_rejects_empty_content() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="empty-1",
        user_id="owner",
        session_id="s",
        content="User said:    ",
        category="general",
    )
    mem.metadata["status"] = "draft"
    with pytest.raises(ValueError, match="empty"):
        build_proposal(state, mem)


def test_build_proposal_rejects_tool_sourced_memory() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="tool-1",
        user_id="owner",
        session_id="s",
        content="User said: web snippet about Paris",
        category="general",
        importance=0.5,
    )
    mem.metadata["source"] = "web_search"
    mem.metadata["status"] = "draft"
    with pytest.raises(ValueError, match="tool-sourced"):
        build_proposal(state, mem)


def test_build_proposal_rejects_missing_provenance_marker() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="bad-1",
        user_id="owner",
        session_id="s",
        content="Just some text without prefix",
        category="general",
        importance=0.5,
    )
    mem.metadata["status"] = "draft"
    with pytest.raises(ValueError, match="user provenance"):
        build_proposal(state, mem)


def test_build_proposal_rejects_missing_status_metadata() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="bad-2",
        user_id="owner",
        session_id="s",
        content="User said: something",
        category="general",
        importance=0.5,
    )
    # no status metadata
    with pytest.raises(ValueError, match="provenance metadata"):
        build_proposal(state, mem)



def test_build_proposal_rejects_forbidden_evidence_kind() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="ev-1",
        user_id="owner",
        session_id="s",
        content="User said: something grounded",
        category="general",
        importance=0.5,
    )
    mem.metadata["status"] = "draft"
    with pytest.raises(ValueError, match="tool-sourced"):
        build_proposal(
            state,
            mem,
            evidence=[{"kind": "web_search", "ref": "hit-1"}],
        )


def test_build_proposal_rejects_snippet_metadata() -> None:
    state = JarvisState(user_id="owner", session_id="s")
    mem = JarvisMemoryEntry(
        memory_id="snip-1",
        user_id="owner",
        session_id="s",
        content="User said: looks grounded",
        category="general",
        importance=0.5,
    )
    mem.metadata["status"] = "draft"
    mem.metadata["snippets"] = ["retrieved hit"]
    with pytest.raises(ValueError, match="tool-sourced"):
        build_proposal(state, mem)


def test_gate_closed_when_writes_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr(settings, "environment", "production")
    gate = check_gate()
    assert gate.allowed is False
    assert "governed_writes_enabled" in gate.reasons or "governed_writes_not_allowed_by_env" in gate.reasons


def test_gate_open_when_writes_allowed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    gate = check_gate(continuity_configured=True, read_only=False)
    assert gate.allowed is True
    assert gate.reasons == []


@pytest.mark.asyncio
async def test_apply_refuses_when_governed_writes_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr(settings, "environment", "production")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "governed_writes_disabled" in result["reasons"]
    engine.continuity.propose_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_refuses_without_explicit_user_request(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=False)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "user_requested_required" in result["reasons"]
    engine.continuity.propose_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_refuses_when_session_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    engine.set_read_only(state.session_id, "conflict")
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "session_read_only" in result["reasons"]
    engine.continuity.propose_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_refuses_when_continuity_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    assert engine.continuity is None
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "continuity_not_configured" in result["reasons"]


@pytest.mark.asyncio
async def test_dry_run_returns_preview_and_audits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal, dry_run=True)

    assert result["status"] == "preview"
    assert result["no_write"] is True
    engine.continuity.propose_memory.assert_not_awaited()
    events = [e for e in engine.get_audit(state.session_id) if e["event_type"] == "memory_promotion_previewed"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_apply_refuses_already_reconciled_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    state.long_term_memory[0].metadata["continuity_ledger_id"] = "ledger-9"
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "memory_already_reconciled" in result["reasons"]
    engine.continuity.propose_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_propose_then_verify_then_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    content = "I prefer concrete builds."
    digest = _sha(f"Preference: {content}")
    engine.continuity = AsyncMock()
    engine.continuity.propose_memory = AsyncMock(
        return_value={
            "status": "accepted",
            "accepted": True,
            "memory": {"id": "ledger-101", "content_sha256": digest},
            "transaction_id": "tx-promote",
            "correlation_id": "corr-promote",
        }
    )
    engine.continuity.retrieve = AsyncMock(return_value={"memory": {"id": "ledger-101", "content_sha256": digest}})
    state = _state_with_draft(engine, content=content)
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "durably_stored"
    assert result["ledger_memory_id"] == "ledger-101"
    assert result["durable"] is True
    assert state.long_term_memory[0].metadata["continuity_ledger_id"] == "ledger-101"
    assert state.long_term_memory[0].metadata["reconciled"] is True
    applied = [e for e in engine.get_audit(state.session_id) if e["event_type"] == "memory_promotion_applied"]
    assert len(applied) == 1
    engine.continuity.propose_memory.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_conflict_sets_read_only_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    engine.continuity.propose_memory = AsyncMock(
        return_value={
            "status": "conflict",
            "refuse_reason": "conflict-membrane",
            "conflicts": [{"memory_id": "other"}],
        }
    )
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "conflict"
    assert result["read_only"] is True
    assert engine.lock_reason(state.session_id) is not None


@pytest.mark.asyncio
async def test_apply_ledger_unavailable_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = _engine(tmp_path)
    engine.continuity = AsyncMock()
    engine.continuity.propose_memory = AsyncMock(return_value={"status": "unavailable"})
    state = _state_with_draft(engine, content="I prefer concrete builds.")
    engine._sessions[state.session_id] = state
    proposal = build_proposal(state, state.long_term_memory[0], user_requested=True)

    result = await apply_promotion(engine, state, proposal)

    assert result["status"] == "refused"
    assert "ledger_unavailable" in result["reasons"]


HEADERS = {"X-Jarvis-Service-Token": "test-service-token"}


def _route_draft(engine, sid: str) -> str:
    state = engine.get_session(sid)
    assert state is not None
    entry = JarvisMemoryEntry(
        memory_id="route-mem-1",
        user_id="owner",
        session_id=sid,
        content="Preference: I prefer concrete builds.",
        category="preference",
        importance=0.8,
    )
    entry.metadata["status"] = "draft"
    state.long_term_memory.append(entry)
    engine._save_session(state)
    return "route-mem-1"


def test_promote_route_refuses_when_governed_writes_disabled(client, monkeypatch):
    client, engine = client
    engine.continuity = AsyncMock()
    response = client.post("/chat", headers=HEADERS, json={"user_id": "owner", "message": "Remember I prefer concrete builds."}).json()
    sid = response["session_id"]
    memory_id = _route_draft(engine, sid)
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr(settings, "environment", "production")

    result = client.post(
        "/memory/promote",
        headers=HEADERS,
        json={
            "user_id": "owner",
            "session_id": sid,
            "memory_id": memory_id,
            "user_requested": True,
        },
    )
    assert result.status_code == 403
    engine.continuity.propose_memory.assert_not_awaited()


def test_promote_route_requires_explicit_user_request(client, monkeypatch):
    client, engine = client
    engine.continuity = AsyncMock()
    response = client.post("/chat", headers=HEADERS, json={"user_id": "owner", "message": "hi"}).json()
    sid = response["session_id"]
    memory_id = _route_draft(engine, sid)
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")

    result = client.post(
        "/memory/promote",
        headers=HEADERS,
        json={
            "user_id": "owner",
            "session_id": sid,
            "memory_id": memory_id,
            "user_requested": False,
        },
    )
    assert result.status_code == 400
    engine.continuity.propose_memory.assert_not_awaited()


def test_promote_preview_route_works_when_writes_disabled(client, monkeypatch):
    client, engine = client
    engine.continuity = AsyncMock()
    response = client.post("/chat", headers=HEADERS, json={"user_id": "owner", "message": "Remember I prefer concrete builds."}).json()
    sid = response["session_id"]
    memory_id = _route_draft(engine, sid)
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr(settings, "environment", "production")

    result = client.post(
        "/memory/promote/preview",
        headers=HEADERS,
        json={
            "user_id": "owner",
            "session_id": sid,
            "memory_id": memory_id,
            "user_requested": True,
        },
    )
    assert result.status_code == 200
    body = result.json()
    assert body["no_write"] is True
    assert body["gate"]["allow_apply"] is False
    engine.continuity.propose_memory.assert_not_awaited()
    engine.continuity.supersede.assert_not_awaited()
