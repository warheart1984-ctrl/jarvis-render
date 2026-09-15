import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.context import build_chat_context
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.inspection import inspect_session
from jarvis.brain.llm import LLMResult
from jarvis.brain.provenance import content_hash, memory_reference
from jarvis.core.config import JarvisSettings, settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState, JarvisMemoryEntry, JarvisState
from jarvis.persistence import JarvisStore
from tests.test_chat_voice import client as client  # noqa: F401

HEADERS = {"X-Jarvis-Service-Token": "test-service-token"}


@pytest.fixture
def inspected(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    generate = AsyncMock(return_value=LLMResult("An accepted answer.", "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    engine = JarvisEngine(store=JarvisStore(tmp_path / "inspection.sqlite3"))
    monkeypatch.setattr(engine, "_sync_with_spiral", AsyncMock())
    return engine, generate


async def chat(engine, **kwargs):
    return await engine.chat(
        ChatRequest(user_id="owner", message="Remember I prefer concrete builds.", **kwargs), recall_owner="owner"
    )


async def saved_draft(engine):
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await chat(engine, session_id=state.session_id, memory_consent=True)
    return response, engine.get_session(state.session_id)


def test_citations_match_actual_bounded_prompt_and_exclude_foreign_records():
    state = JarvisState(user_id="owner", session_id="s")
    state.long_term_memory = [
        JarvisMemoryEntry(memory_id="real-id", user_id="owner", session_id="s", content="é" * 500),
        JarvisMemoryEntry(memory_id="private-id", user_id="other", session_id="s", content="PRIVATE"),
    ]
    state.conversation_history = [{"role": "user", "content": "Old conversation", "turn_id": "old-turn"}]
    messages, facts = build_chat_context(
        state,
        ChatRequest(user_id="owner", message="Now"),
        read_only=True,
        infinity_configured=False,
        continuity_configured=False,
        speech_configured=False,
    )
    citations = facts["prepared_citations"]
    assert len(citations) == 2
    memory, history = citations
    excerpt = json.loads(messages[1]["content"].split("\n", 1)[1])[0]["content"]
    assert memory["memory_id"] == "real-id"
    assert memory["content_sha256"] == content_hash("é" * 500)
    assert memory["excerpt_sha256"] == content_hash(excerpt)
    assert memory["truncated"] and memory["excerpt_chars"] == 400
    assert history["memory_id"] is None and history["message_ref"] == "old-turn"
    assert "PRIVATE" not in json.dumps(messages) and "private-id" not in json.dumps(citations)
    assert "é" not in json.dumps(citations, ensure_ascii=False)


@pytest.mark.asyncio
async def test_draft_record_and_exact_receipt_survive_restart_without_external_writes(inspected):
    engine, generate = inspected
    first, state = await saved_draft(engine)
    assert first.context_receipt["citations"] == []  # Current write did not cause the answer.
    memory = state.long_term_memory[0]
    assert memory.metadata["status"] == "draft"
    assert engine.store.inspect_memories(state.session_id)[0]["status"] == "draft"
    assert not engine.store.recall_memories(state.session_id)  # Not promoted to active.
    second = await chat(engine, session_id=state.session_id)
    citations = second.context_receipt["citations"]
    assert len(citations) == 3  # One extracted record, two history messages.
    assert citations[0]["memory_id"] == memory.memory_id
    engine._sync_with_spiral.assert_not_awaited()
    assert memory.content in json.dumps(generate.call_args.args[0])
    snapshot = inspect_session(engine, engine.get_session(state.session_id))
    assert snapshot["records"][0]["integrity"] == "audit_bound"
    assert snapshot["records"][0]["amul_artifact"] is None
    assert snapshot["turns"][-1]["context_receipt"] == second.context_receipt
    assert snapshot["turns"][-1]["transaction_id"] == second.transaction_id
    assert snapshot["turns"][-1]["correlation_id"] == second.correlation_id
    assert memory.content not in json.dumps(engine.get_audit(state.session_id))
    restarted = JarvisEngine(store=engine.store)
    restored = await restarted.get_or_create_session("owner", state.session_id)
    assert restarted.is_read_only(state.session_id)
    after = inspect_session(restarted, restored)
    assert after["session_read_only"] is True
    assert after["lock_reason"] == "recovery"
    for key in ("session_read_only", "lock_reason"):
        snapshot.pop(key, None)
        after.pop(key, None)
    assert after == snapshot
    assert restored.conversation_history[-1]["turn_id"] == second.turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["content", "hash", "status", "metadata"])
async def test_changed_memory_is_withheld(inspected, target):
    engine, _ = inspected
    _, state = await saved_draft(engine)
    with sqlite3.connect(engine.store.path) as db:
        if target == "content":
            db.execute("UPDATE memories SET content='TAMPERED'")
        elif target == "hash":
            db.execute("UPDATE memories SET content_sha256='bad'")
        elif target == "status":
            db.execute("UPDATE memories SET status='active'")
        else:
            state.long_term_memory[0].metadata["amul_artifact"] = "fabricated"
    data = inspect_session(engine, state)
    assert data["records"] == [] and data["withheld_records"] == 1
    assert "TAMPERED" not in json.dumps(data) and "fabricated" not in json.dumps(data)


@pytest.mark.asyncio
async def test_bad_audit_withholds_records_and_receipts(inspected):
    engine, _ = inspected
    _, state = await saved_draft(engine)
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE audit_events SET payload_json='{}'")
    data = inspect_session(engine, state)
    assert data["status"] == "unverified" and not data["records"] and not data["turns"]


@pytest.mark.asyncio
async def test_receipts_come_from_audit_not_mutable_trace_json(inspected):
    engine, _ = inspected
    _, state = await saved_draft(engine)
    response = await chat(engine, session_id=state.session_id)
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE spiral_turns SET evidence_json='[]'")
    assert (
        inspect_session(engine, engine.get_session(state.session_id))["turns"][-1]["context_receipt"]
        == response.context_receipt
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unavailable", "refused", "unknown", "local", "stress"])
async def test_no_inference_never_claims_context_influence(inspected, monkeypatch, outcome):
    engine, generate = inspected
    _, state = await saved_draft(engine)
    generate.return_value = (
        None
        if outcome == "local"
        else LLMResult("Safe status only", "internal", "minimal", 0, safe_mode=True, inference_status=outcome)
    )
    if outcome == "stress":
        monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(stress=0.9))
    response = await chat(engine, session_id=state.session_id)
    assert response.context_receipt["citations"] == []
    assert response.context_receipt["status"] in {"no_inference", "not_requested"}


@pytest.mark.asyncio
async def test_previous_records_require_exact_verified_checkpoint_and_opt_in(inspected):
    engine, _ = inspected
    first, old_state = await saved_draft(engine)
    second = await chat(engine, recall_previous=True)
    data = inspect_session(engine, engine.get_session(second.session_id))
    assert data["records"][0]["session_id"] == first.session_id
    assert data["records"][0]["checkpoint_id"] == second.previous_session["checkpoint_id"]
    assert {c["session_id"] for c in second.context_receipt["citations"]} == {first.session_id}
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE recall_checkpoints SET signature='invalid' WHERE session_id=?", (first.session_id,))
    data = inspect_session(engine, engine.get_session(second.session_id))
    assert not data["records"] and data["previous_session"]["status"] == "unverified"
    third = await chat(engine)  # Inspection is not an alternate way to opt into prior recall.
    assert not inspect_session(engine, engine.get_session(third.session_id))["records"]


def test_artifact_mapping_is_optional_narrow_and_not_claimed_verified():
    memory = JarvisMemoryEntry(
        memory_id="m",
        user_id="u",
        session_id="s",
        content="x",
        metadata={
            "amul_artifact": {"artifact_id": "artifact-1", "content_sha256": "a" * 64, "token": "PRIVATE"},
            "continuity_ledger_id": "ledger-1",
            "api_key": "PRIVATE",
        },
    )
    ref = memory_reference(memory)
    assert ref["amul_artifact"] == {"artifact_id": "artifact-1", "content_sha256": "a" * 64}
    assert ref["artifact_verification"] == "not_checked" and ref["ledger_memory_id"] == "ledger-1"
    assert "PRIVATE" not in json.dumps(ref)


@pytest.mark.asyncio
async def test_legacy_record_and_amul_are_inspectable_without_inventing_attestation(inspected):
    engine, _ = inspected
    state = await engine.get_or_create_session("owner")
    memory = JarvisMemoryEntry(
        memory_id="legacy-memory",
        user_id="owner",
        session_id=state.session_id,
        content="Legacy example",
        metadata={"amul_artifact": {"artifact_id": "amul-example"}},
    )
    state.long_term_memory.append(memory)
    engine.store.save_memory(
        {
            "id": memory.memory_id,
            "session_id": state.session_id,
            "subject": "owner",
            "content": memory.content,
            "created_at": memory.created_at,
        }
    )
    result = await chat(engine, session_id=state.session_id)
    record = inspect_session(engine, engine.get_session(state.session_id))["records"][0]
    assert record["status"] == "legacy_unreviewed" and record["storage_status"] == "active"
    assert record["integrity"] == "hash_matches_storage"
    assert record["amul_artifact"] == {"artifact_id": "amul-example"}
    assert result.context_receipt["citations"][0]["amul_artifact"] == record["amul_artifact"]
    assert result.context_receipt["citations"][0]["artifact_verification"] == "not_checked"


@pytest.mark.asyncio
async def test_unrecorded_legacy_turn_is_not_given_invented_citations(inspected):
    from datetime import datetime, timezone

    from jarvis.models.jarvis_types import SpiralTurn

    engine, _ = inspected
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
    assert data["turns"][0]["context_receipt"] == {"status": "not_recorded", "citations": []}
    assert data["turns"][0]["deliberation"]["status"] == "not_recorded"
    assert data["turns"][0]["cer"] == {"status": "not_recorded"}


def test_inspection_auth_ownership_and_production_write_gate(client, monkeypatch):  # noqa: F811
    client, engine = client
    response = client.post("/chat", headers=HEADERS, json={"user_id": "owner", "message": "hi"}).json()
    sid = response["session_id"]
    path = f"/sessions/{sid}/memory-inspection?user_id=owner"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=HEADERS).json()["read_only"] is True
    assert client.get(path.replace("owner", "other"), headers=HEADERS).status_code == 404
    monkeypatch.setattr(settings, "recall_owner_user_id", "bound-owner")
    assert client.get(path, headers=HEADERS).status_code == 403
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    assert client.get("/capabilities", headers=HEADERS).json()["governed_writes_enabled"] is False
    engine.continuity = AsyncMock()
    for path in ["/memory/propose", "/memory/supersede"]:
        result = client.post(
            path,
            headers=HEADERS,
            json={
                "user_id": "owner",
                "session_id": sid,
                "memory_id": "m",
                "user_requested": True,
                "content": "replacement",
            },
        )
        assert result.status_code == 403
    engine.continuity.propose_memory.assert_not_awaited()
    engine.continuity.supersede.assert_not_awaited()


def test_governed_writes_off_by_default():
    config = JarvisSettings(_env_file=None, governed_writes_enabled=False)
    assert not config.governed_writes_allowed()


@pytest.mark.asyncio
async def test_cleared_memory_is_not_resurrected_by_inspection(inspected):
    engine, _ = inspected
    _, state = await saved_draft(engine)
    engine.clear_memory(state.session_id)
    data = inspect_session(engine, state)
    assert data["status"] == "withheld" and not data["records"] and not data["turns"]
