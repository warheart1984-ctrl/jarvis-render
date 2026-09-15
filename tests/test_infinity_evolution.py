"""Project Infinity is the bounded evolve path; Spiral V8 is not the evolution backend."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState
from jarvis.persistence import JarvisStore
from jarvis.persistence.tenancy import LEGACY_SUBJECT, LEGACY_TENANT


@pytest.mark.asyncio
async def test_infinity_evolve_replaces_spiral_placeholder_and_is_evidence_only(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "infinity_enabled", True)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("Keep this reply.", "test", "model", 1)),
    )
    engine = JarvisEngine(store=JarvisStore(tmp_path / "infinity.sqlite3", LEGACY_TENANT, LEGACY_SUBJECT))
    engine.infinity = AsyncMock()
    engine.infinity.evolve = AsyncMock(return_value={"ok": True, "best": "secret-candidate"})
    engine.spiral = AsyncMock()
    engine.spiral.is_available = AsyncMock(return_value=True)
    engine.spiral.spiral_turn = AsyncMock()
    engine.spiral.spiral_chat = AsyncMock()
    engine.spiral.write_memory = AsyncMock()

    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(
        ChatRequest(
            user_id="owner",
            message="Improve this",
            session_id=state.session_id,
            memory_consent=True,
        )
    )

    assert response.reply == "Keep this reply."
    assert "secret-candidate" not in response.reply
    engine.infinity.evolve.assert_awaited_once()
    engine.spiral.spiral_turn.assert_not_awaited()
    engine.spiral.spiral_chat.assert_not_awaited()
    engine.spiral.write_memory.assert_not_awaited()
    turns = engine.get_trace(response.session_id)
    assert turns[0]["backend_status"] == "connected"
    events = [e for e in engine.get_audit(response.session_id) if e["event_type"] == "spiral_turn"]
    payload = json.loads(events[-1]["payload_json"])
    assert payload["backend_status"] == "connected"
    suggestions = payload.get("external_suggestions") or []
    assert suggestions
    assert suggestions[0]["source"] == "project_infinity"
    assert suggestions[0]["authority"] is False


@pytest.mark.asyncio
async def test_sync_without_infinity_does_not_call_spiral_memory_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    engine = JarvisEngine(store=JarvisStore(tmp_path / "no-inf.sqlite3", LEGACY_TENANT, LEGACY_SUBJECT))
    engine.infinity = None
    engine.spiral = AsyncMock()
    engine.spiral.is_available = AsyncMock(return_value=True)
    engine.spiral.write_memory = AsyncMock()
    state = await engine.get_or_create_session("owner")
    status, payload = await engine._sync_with_spiral(state, "task", "draft")
    assert status == "skipped_no_infinity"
    assert payload is None
    engine.spiral.write_memory.assert_not_awaited()
