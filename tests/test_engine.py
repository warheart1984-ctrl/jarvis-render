"""Tests for the Jarvis engine — conversation loop, spiral evolution, and memory."""

from __future__ import annotations

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.models.jarvis_types import ChatRequest
from jarvis.persistence import JarvisStore


@pytest.fixture
def engine(tmp_path) -> JarvisEngine:
    return JarvisEngine(store=JarvisStore(tmp_path / "engine.sqlite3"))


@pytest.mark.asyncio
async def test_basic_chat(engine: JarvisEngine) -> None:
    request = ChatRequest(user_id="test-user", message="Hello Jarvis, what can you do?")
    response = await engine.chat(request)

    assert response.session_id
    assert response.reply
    assert response.energy > 0
    assert response.confidence > 0
    assert response.spiral_state


@pytest.mark.asyncio
async def test_session_persistence(engine: JarvisEngine) -> None:
    req1 = ChatRequest(user_id="test-user", message="Let's build something.")
    resp1 = await engine.chat(req1)

    req2 = ChatRequest(
        user_id="test-user",
        message="I want to create an evolving AI.",
        session_id=resp1.session_id,
    )
    resp2 = await engine.chat(req2)

    assert resp2.session_id == resp1.session_id
    assert resp2.memory_snapshot["conversation_turns"] == 2


@pytest.mark.asyncio
async def test_spiral_evolution(engine: JarvisEngine) -> None:
    req1 = ChatRequest(user_id="test-user", message="Start exploring.")
    resp1 = await engine.chat(req1)
    initial_angle = resp1.spiral_state["angle"]

    req2 = ChatRequest(
        user_id="test-user",
        message="Build me a backend system now!",
        session_id=resp1.session_id,
    )
    resp2 = await engine.chat(req2)

    # The spiral angle should have advanced.
    assert resp2.spiral_state["angle"] != initial_angle


@pytest.mark.asyncio
async def test_emotion_detection(engine: JarvisEngine) -> None:
    request = ChatRequest(
        user_id="test-user",
        message="This is broken and I'm frustrated! Fix it now!",
    )
    response = await engine.chat(request)

    # Should detect stress / frustration signals.
    assert response.emotion["stress"] > 0


@pytest.mark.asyncio
async def test_memory_extraction(engine: JarvisEngine) -> None:
    state = await engine.get_or_create_session("test-user")
    state.confidence = 0.8
    request = ChatRequest(
        user_id="test-user",
        message="I always prefer concrete builds over abstract theory. Remember that.",
        session_id=state.session_id,
        memory_consent=True,
    )
    response = await engine.chat(request)

    assert response.memory_snapshot["long_term_entries"] >= 1
    assert response.memory_snapshot["preferences"].get("prefers_concrete_builds") == "true"


@pytest.mark.asyncio
async def test_state_retrieval(engine: JarvisEngine) -> None:
    request = ChatRequest(user_id="test-user", message="Hello")
    response = await engine.chat(request)

    state = engine.get_state_summary(response.session_id)
    assert state["session_id"] == response.session_id
    assert state["turn_count"] == 1


@pytest.mark.asyncio
async def test_memory_clear(engine: JarvisEngine) -> None:
    request = ChatRequest(user_id="test-user", message="Remember this important thing about spiral AI.")
    response = await engine.chat(request)

    result = engine.clear_memory(response.session_id)
    assert result["status"] == "cleared"

    memory = engine.get_memory_summary(response.session_id)
    assert len(memory["conversation_history"]) == 0
    assert len(memory["long_term_memory"]) == 0
