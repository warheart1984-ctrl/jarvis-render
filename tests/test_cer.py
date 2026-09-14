"""CER completeness lives on the existing spiral_turn audit — no fifth ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.cer import CER_VERSION, build_cer_record
from jarvis.brain.deliberation import deliberate
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.inspection import inspect_session
from jarvis.brain.llm import LLMResult
from jarvis.brain.provenance import content_hash
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState, SpiralTurn
from jarvis.persistence import JarvisStore


def _cer_input(**overrides):
    deliberation = deliberate("The sky might be cloudy today.", user_message="Weather?", fail_closed=False)
    payload = {
        "session_id": "sess-1",
        "turn_id": "turn-2",
        "transaction_id": "turn-2",
        "correlation_id": "corr-2",
        "user_message": "Weather?",
        "reply": "The sky might be cloudy today.",
        "provider": "test",
        "model": "model",
        "inference_status": "accepted",
        "fallback_used": False,
        "safe_mode": False,
        "deliberation": deliberation,
        "claims": deliberation["claims"],
        "observe": {
            "required": False,
            "thin": True,
            "records": [
                {
                    "tool": "web_search",
                    "status": "ok",
                    "observed": True,
                    "args_hash": "aa",
                    "payload_hash": "bb",
                    "args": {"query": "weather", "api_key": "SECRET-KEY"},
                    "citations": [{"locator": "https://example.test/w", "content_sha256": "cc", "snippet": "rain"}],
                }
            ],
        },
        "context_receipt": {"status": "included", "citations": []},
        "previous_turn_id": "turn-1",
    }
    payload.update(overrides)
    return payload


def test_cer_record_has_identity_plan_evidence_verification_replay_and_lineage() -> None:
    record = build_cer_record(**_cer_input())
    assert record["version"] == CER_VERSION
    assert record["schema"] == "constitutional_execution_record"
    assert set(record) >= {
        "version",
        "schema",
        "model_identity",
        "plan",
        "evidence",
        "verification",
        "replay",
        "lineage",
    }
    assert record["model_identity"] == {
        "provider": "test",
        "model": "model",
        "inference_status": "accepted",
        "fallback_used": False,
        "safe_mode": False,
    }
    names = [stage["name"] for stage in record["plan"]["stages"]]
    assert names[0] == "observe"
    assert "infer" in names and "challenge" in names and "commit" in names
    assert record["plan"]["committed"] is True
    assert record["verification"]["content_sha256"] == content_hash("The sky might be cloudy today.")
    assert record["verification"]["input_sha256"] == content_hash("Weather?")
    assert record["verification"]["challenge"] == "completed"
    assert record["replay"]["transaction_id"] == "turn-2"
    assert record["replay"]["correlation_id"] == "corr-2"
    assert record["lineage"]["previous_turn_id"] == "turn-1"
    assert record["lineage"]["session_id"] == "sess-1"
    assert record["lineage"]["turn_id"] == "turn-2"


def test_cer_evidence_uses_hashes_and_omits_secrets() -> None:
    dumped = json.dumps(build_cer_record(**_cer_input()))
    assert "SECRET-KEY" not in dumped
    assert "api_key" not in dumped
    tools = build_cer_record(**_cer_input())["evidence"]["observe_tools"]
    assert tools[0]["args_hash"] == "aa"
    assert tools[0]["payload_hash"] == "bb"
    assert tools[0]["citations"][0]["locator"] == "https://example.test/w"
    assert tools[0]["citations"][0]["content_sha256"] == "cc"
    assert "snippet" not in tools[0]["citations"][0]


def test_cer_does_not_store_hidden_reasoning_markup() -> None:
    dumped = json.dumps(build_cer_record(**_cer_input()))
    assert "<think" not in dumped.lower()
    assert "cot" not in dumped.lower()


@pytest.fixture
def cer_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    generate = AsyncMock(return_value=LLMResult("The sky might be cloudy today.", "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    engine = JarvisEngine(store=JarvisStore(tmp_path / "cer.sqlite3"))
    monkeypatch.setattr(engine, "_sync_with_spiral", AsyncMock(return_value=("skipped", None)))
    return engine


@pytest.mark.asyncio
async def test_engine_persists_cer_on_existing_spiral_turn_audit(cer_engine) -> None:
    engine = cer_engine
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    first = await engine.chat(ChatRequest(user_id="owner", message="Hello there", session_id=state.session_id))
    second = await engine.chat(
        ChatRequest(user_id="owner", message="Weather?", session_id=state.session_id)
    )
    events = [e for e in engine.get_audit(state.session_id) if e["event_type"] == "spiral_turn"]
    assert len(events) == 2
    payload = json.loads(events[-1]["payload_json"])
    cer = payload["cer"]
    assert cer["version"] == CER_VERSION
    assert cer["model_identity"]["provider"] == second.provider
    assert cer["model_identity"]["model"] == second.model
    assert cer["replay"]["transaction_id"] == second.transaction_id
    assert cer["replay"]["correlation_id"] == second.correlation_id
    assert cer["verification"]["content_sha256"] == content_hash(second.reply)
    assert cer["verification"]["input_sha256"] == content_hash("Weather?")
    assert cer["lineage"]["previous_turn_id"] == first.turn_id
    assert second.cer == cer
    tables = {
        row[0]
        for row in sqlite3.connect(engine.store.path).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "cer_records" not in tables
    assert "constitutional_execution" not in tables


@pytest.mark.asyncio
async def test_inspection_reads_cer_from_audit_not_mutable_trace(cer_engine) -> None:
    engine = cer_engine
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(ChatRequest(user_id="owner", message="Weather?", session_id=state.session_id))
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE spiral_turns SET evidence_json='[]'")
    snapshot = inspect_session(engine, engine.get_session(state.session_id))
    last = snapshot["turns"][-1]
    assert last["cer"] == response.cer
    assert last["cer"]["schema"] == "constitutional_execution_record"


@pytest.mark.asyncio
async def test_legacy_turn_without_cer_is_not_recorded(cer_engine) -> None:
    engine = cer_engine
    state = await engine.get_or_create_session("owner")
    turn = SpiralTurn(
        turn_id="legacy-turn",
        session_id=state.session_id,
        timestamp=datetime.now(timezone.utc),
        decision="answer",
        uncertainty=0.1,
        stress=0,
        content="Legacy answer",
        content_sha256=content_hash("Legacy answer"),
    )
    engine.store.save_turn_bundle(
        turn,
        {
            "event_id": "legacy",
            "event_type": "spiral_turn",
            "timestamp": turn.timestamp.isoformat(),
            "payload": {"content_sha256": turn.content_sha256},
        },
    )
    data = inspect_session(engine, state)
    assert data["turns"][0]["cer"] == {"status": "not_recorded"}
