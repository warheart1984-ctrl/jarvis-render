"""Durable write-path lock: hypothesized / tool / inferred summaries stay off memory."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.deliberation import DeliberationRunner, EvidenceKind, hypothesized_none
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.brain.memory import extract_memory
from jarvis.brain.tools import FakeSearchBackend, may_admit_retrieved_to_memory
from jarvis.brain.write_lock import WRITE_PATH_LOCK_SHA256, WRITE_PATH_LOCK_TUPLE
from jarvis.core.config import JarvisSettings, settings
from jarvis.models.jarvis_types import ChatRequest, JarvisState
from jarvis.persistence import JarvisStore

PARIS_HIT = {
    "url": "https://example.test/paris",
    "title": "Paris",
    "excerpt": "Paris is the capital of France.",
}
PARIS_SUMMARY = "Paris is the capital of France."
README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_hash_matches_canonical_lock_tuple() -> None:
    digest = hashlib.sha256(WRITE_PATH_LOCK_TUPLE.encode("utf-8")).hexdigest()
    assert digest == WRITE_PATH_LOCK_SHA256
    assert WRITE_PATH_LOCK_SHA256 == "02c03803b2181aeaeb2429d30102802f9094348b32569284bd4a0c2c566e3da3"
    text = README.read_text(encoding="utf-8")
    assert WRITE_PATH_LOCK_SHA256 in text
    assert "product lock" in text.lower()
    assert "not a security audit" in text.lower()
    assert "not otem" in text.lower()


def test_production_env_cannot_enable_governed_writes() -> None:
    config = JarvisSettings(
        _env_file=None,
        environment="production",
        service_token="deployment-token",
        governed_writes_enabled=True,
        cors_origins="https://jarvis.example",
        public_origin="https://jarvis.example",
        auth_mode="operator",
    )
    assert config.governed_writes_allowed() is False
    assert may_admit_retrieved_to_memory(user_requested=True) is False


def test_extract_memory_refuses_tool_snippets_and_ineligible_admission() -> None:
    state = JarvisState(user_id="u", session_id="s")
    state.confidence = 0.9
    user = "I always prefer concrete builds over abstract theory. Remember that."
    admitted = extract_memory(state, user, "I'll remember your preference for concrete builds.")
    assert admitted is not None
    assert PARIS_SUMMARY not in admitted.content
    assert "I'll remember" not in admitted.content
    assert (
        extract_memory(
            state,
            user,
            PARIS_SUMMARY,
            snippets=[PARIS_SUMMARY],
        )
        is None
    )
    assert extract_memory(state, user, PARIS_SUMMARY, memory_admission="blocked") is None


def test_hypothesized_reply_claim_is_not_memory_eligible() -> None:
    runner = DeliberationRunner()
    runner.add_evidence(hypothesized_none())
    runner.observe(message="What should I remember about Europe?", session_id="s1", cite_utterance=False)
    runner.interpret(emotion_label="curious", intent="transform", phase="reason", confidence=0.7)
    runner.infer()
    runner.challenge(uncertainty=0.3, stress=0.1)
    runner.evaluate(PARIS_SUMMARY)
    result = runner.commit()
    assert result.memory_admission == "blocked"
    assert result.memory_admission != "eligible"
    assert any(item.kind is EvidenceKind.HYPOTHESIZED_NONE for item in result.evidence)


@pytest.mark.asyncio
async def test_polished_search_summary_is_not_stored_memory(tmp_path, monkeypatch) -> None:
    engine = JarvisEngine(store=JarvisStore(tmp_path / "search-lock.sqlite3"))
    engine.search_backend = FakeSearchBackend(hits=[PARIS_HIT])
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult(PARIS_SUMMARY, "test", "text-model", 12.5)),
    )
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.84
    response = await engine.chat(
        ChatRequest(
            user_id="owner",
            session_id=state.session_id,
            message="Search for the capital of France",
            memory_consent=True,
        )
    )
    stored = engine.get_session(response.session_id)
    assert stored is not None
    blob = " ".join(entry.content for entry in stored.long_term_memory)
    assert PARIS_SUMMARY not in blob
    assert "Paris" not in blob
    assert response.deliberation["memory_admission"] == "blocked"
    assert response.memory_snapshot["long_term_entries"] == 0
    assert response.tool_calls[0]["memory_written"] is False
    assert may_admit_retrieved_to_memory(user_requested=True) is False


@pytest.mark.asyncio
async def test_hypothesized_engine_reply_does_not_write_memory(tmp_path, monkeypatch) -> None:
    engine = JarvisEngine(store=JarvisStore(tmp_path / "hypothesized-lock.sqlite3"))
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult(PARIS_SUMMARY, "test", "text-model", 12.5)),
    )
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.84
    response = await engine.chat(
        ChatRequest(
            user_id="owner",
            session_id=state.session_id,
            message="What should I remember about Europe this week?",
            memory_consent=True,
        )
    )
    stored = engine.get_session(response.session_id)
    assert stored is not None
    assert response.deliberation["memory_admission"] == "blocked"
    assert all(PARIS_SUMMARY not in entry.content for entry in stored.long_term_memory)
    assert all("Paris" not in entry.content for entry in stored.long_term_memory)


@pytest.mark.asyncio
async def test_production_flag_cannot_write_tool_or_hypothesized_memory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    assert settings.governed_writes_allowed() is False
    engine = JarvisEngine(store=JarvisStore(tmp_path / "prod-lock.sqlite3"))
    engine.continuity = AsyncMock()
    engine.search_backend = FakeSearchBackend(hits=[PARIS_HIT])
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult(PARIS_SUMMARY, "test", "text-model", 12.5)),
    )
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.84
    response = await engine.chat(
        ChatRequest(
            user_id="owner",
            session_id=state.session_id,
            message="Search for the capital of France",
            memory_consent=True,
        )
    )
    stored = engine.get_session(response.session_id)
    assert stored is not None
    assert response.deliberation["memory_admission"] == "blocked"
    assert response.memory_snapshot["long_term_entries"] == 0
    assert all(PARIS_SUMMARY not in entry.content for entry in stored.long_term_memory)
    engine.continuity.propose_memory.assert_not_awaited()
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    response_dev = await engine.chat(
        ChatRequest(
            user_id="owner",
            session_id=state.session_id,
            message="Search for the capital of France again",
            memory_consent=True,
        )
    )
    stored_dev = engine.get_session(response_dev.session_id)
    assert stored_dev is not None
    assert response_dev.deliberation["memory_admission"] == "blocked"
    assert all(PARIS_SUMMARY not in entry.content for entry in stored_dev.long_term_memory)
    assert may_admit_retrieved_to_memory(user_requested=True) is False
