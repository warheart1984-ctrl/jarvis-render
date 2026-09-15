"""Authenticated supersession archives locally and unlocks only after confirmed success."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.core.config import settings
from jarvis.models.jarvis_types import JarvisMemoryEntry
from jarvis.persistence import JarvisStore


@pytest.fixture
def supersede_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    engine = JarvisEngine(store=JarvisStore(tmp_path / "supersede.sqlite3"))
    engine.continuity = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_apply_supersession_archives_old_writes_lineage_audits_then_unlocks(supersede_engine):
    engine = supersede_engine
    state = await engine.get_or_create_session("owner")
    memory = JarvisMemoryEntry(
        memory_id="mem-old",
        user_id="owner",
        session_id=state.session_id,
        content="old claim",
        metadata={"continuity_ledger_id": "ledger-old", "status": "draft"},
    )
    state.long_term_memory.append(memory)
    engine.store.save_memory(
        {
            "id": memory.memory_id,
            "session_id": state.session_id,
            "subject": "owner",
            "content": memory.content,
            "created_at": memory.created_at,
            "status": "draft",
        }
    )
    engine.set_read_only(state.session_id, "conflict")

    result = {
        "accepted": True,
        "memory": {"id": "ledger-new", "content_sha256": "a" * 64},
        "lineage": {"supersedes": "ledger-old"},
    }
    applied = engine.apply_supersession(
        state,
        memory_id=memory.memory_id,
        content="replacement claim",
        ledger_result=result,
    )
    assert applied["status"] == "superseded"
    assert engine.is_read_only(state.session_id) is False
    refreshed = engine.get_session(state.session_id)
    assert refreshed is not None
    old = next(m for m in refreshed.long_term_memory if m.memory_id == "mem-old")
    assert old.metadata["status"] == "superseded"
    new = next(m for m in refreshed.long_term_memory if m.memory_id != "mem-old")
    assert new.content == "replacement claim"
    assert new.metadata["continuity_ledger_id"] == "ledger-new"
    assert new.metadata["supersedes"] == "mem-old"
    assert new.metadata["lineage"]["supersedes"] == "ledger-old"
    events = [e for e in engine.get_audit(state.session_id) if e["event_type"] == "memory_supersession"]
    assert len(events) == 1
    stored = {row["id"]: row for row in engine.store.inspect_memories(state.session_id)}
    assert stored["mem-old"]["status"] == "superseded"
    assert stored[new.memory_id]["status"] == "draft"


@pytest.mark.asyncio
async def test_apply_supersession_refuses_invalid_lineage_and_keeps_lock(supersede_engine):
    engine = supersede_engine
    state = await engine.get_or_create_session("owner")
    memory = JarvisMemoryEntry(
        memory_id="mem-old",
        user_id="owner",
        session_id=state.session_id,
        content="old claim",
        metadata={"continuity_ledger_id": "ledger-old"},
    )
    state.long_term_memory.append(memory)
    engine.set_read_only(state.session_id, "conflict")
    with pytest.raises(ValueError, match="lineage"):
        engine.apply_supersession(
            state,
            memory_id="mem-old",
            content="replacement",
            ledger_result={"accepted": True, "memory": {"id": "ledger-old"}, "lineage": {}},
        )
    assert engine.is_read_only(state.session_id) is True
    assert engine.get_audit(state.session_id) == []
