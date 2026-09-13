import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.context import MAX_RECALL_MESSAGE_CHARS, MAX_RECALL_MESSAGES, build_chat_context
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, JarvisMemoryEntry, JarvisState
from jarvis.persistence import JarvisStore
from jarvis.persistence.recall import RecallResult
from tests.test_chat_voice import client as client  # noqa: F401

KEY = "test-service-token"
HEADERS = {"X-Jarvis-Service-Token": KEY}


@pytest.fixture
def recall_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "service_token", KEY)
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    generate = AsyncMock(return_value=LLMResult("A contextual answer.", "test", "text-model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    engine = JarvisEngine(store=JarvisStore(tmp_path / "recall.sqlite3"))
    return engine, generate


async def turn(engine, *, owner="owner", message="Spiral is my software reasoning backend.", **kwargs):
    return await engine.chat(ChatRequest(user_id=owner, message=message, **kwargs), recall_owner=owner)


def previous(engine, owner="owner", key=KEY, sid="new-session", before="9999"):
    return engine.recall.previous(owner, key, session_id=sid, before=before)


def snapshot(engine, sid):
    with sqlite3.connect(engine.store.path) as db:
        return [
            db.execute(f"SELECT * FROM {table} WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
            for table in ("spiral_turns", "audit_events", "reviver_checkpoints", "memories", "recall_checkpoints")
        ]


@pytest.mark.asyncio
async def test_new_session_after_restart_uses_signed_context_without_mutating_source(recall_engine):
    engine, generate = recall_engine
    first = await turn(engine)
    original = snapshot(engine, first.session_id)
    restarted = JarvisEngine(store=engine.store)
    await restarted.get_or_create_session("owner", first.session_id)
    assert restarted.is_read_only(first.session_id)
    second = await turn(restarted, message="What did we talk about last session?", recall_previous=True)
    assert second.session_id != first.session_id
    assert second.previous_session["status"] == "verified"
    assert second.previous_session["source_session_id"] == first.session_id
    messages = generate.call_args.args[0]
    assert "Spiral is my software reasoning backend." in messages[1]["content"]
    assert messages[1]["role"] == "user"
    assert "Spiral is my software reasoning backend." not in messages[0]["content"]
    assert KEY not in json.dumps(messages)
    assert snapshot(engine, first.session_id) == original
    assert restarted.is_read_only(first.session_id)
    event = next(e for e in restarted.get_audit(second.session_id) if e["event_type"] == "previous_session_recall")
    payload = json.loads(event["payload_json"])
    assert payload["transaction_id"] == second.transaction_id
    assert payload["correlation_id"] == second.correlation_id
    assert payload["checkpoint_id"] == second.previous_session["checkpoint_id"]
    assert "Spiral is my software reasoning backend." not in event["payload_json"]
    assert restarted.audit.verify(second.session_id)


@pytest.mark.asyncio
async def test_opt_out_and_client_context_cannot_enable_or_select_recall(recall_engine):
    engine, generate = recall_engine
    await turn(engine)
    result = await turn(engine, message="Hello", context={"recall_owner": "owner", "recall_previous": True})
    assert result.previous_session == {"status": "disabled"}
    assert "Spiral is my software reasoning backend." not in json.dumps(generate.call_args.args[0])
    assert not any(e["event_type"] == "previous_session_recall" for e in engine.get_audit(result.session_id))
    assert previous(engine, owner="other").state is None
    result = await engine.chat(ChatRequest(user_id="owner", message="Hi", recall_previous=True))
    assert result.previous_session["status"] == "not_authorized"


@pytest.mark.asyncio
async def test_foreign_newer_session_excluded_and_source_stable(recall_engine, monkeypatch):
    engine, _ = recall_engine
    old = await turn(engine)
    current = await engine.get_or_create_session("owner")
    newer = await turn(engine, message="A later unrelated conversation")
    monkeypatch.setattr(settings, "recall_owner_user_id", "other")
    foreign = await turn(engine, owner="other", message="PRIVATE_OTHER_USER")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    result = await turn(engine, session_id=current.session_id, recall_previous=True)
    assert result.previous_session["source_session_id"] == old.session_id
    assert result.previous_session["source_session_id"] not in (newer.session_id, foreign.session_id)
    assert "PRIVATE_OTHER_USER" not in json.dumps(previous(engine).state.model_dump())
    assert previous(engine, sid=old.session_id, before=current.created_at).state is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["audit", "state", "state_owner", "index_owner", "signature", "json", "newer_unsealed"]
)
async def test_tamper_withholds_latest_without_falling_back_to_older(recall_engine, target):
    engine, _ = recall_engine
    await turn(engine, message="Older valid source")
    latest = await turn(engine, message="LATEST_PRIVATE")
    with sqlite3.connect(engine.store.path) as db:
        if target == "audit":
            db.execute("UPDATE audit_events SET payload_json='{}' WHERE session_id=?", (latest.session_id,))
        elif target in {"state", "state_owner"}:
            row = db.execute(
                "SELECT state_json FROM reviver_checkpoints WHERE session_id=?", (latest.session_id,)
            ).fetchone()
            state = json.loads(row[0])
            if target == "state_owner":
                state["user_id"] = "forged-owner"
            else:
                state["conversation_history"][0]["content"] = "TAMPERED"
            db.execute(
                "UPDATE reviver_checkpoints SET state_json=? WHERE session_id=?", (json.dumps(state), latest.session_id)
            )
        elif target == "index_owner":
            db.execute("UPDATE recall_checkpoints SET owner='victim' WHERE session_id=?", (latest.session_id,))
        elif target == "signature":
            db.execute("UPDATE recall_checkpoints SET signature='forged' WHERE session_id=?", (latest.session_id,))
        elif target == "json":
            db.execute("UPDATE reviver_checkpoints SET state_json='not-json' WHERE session_id=?", (latest.session_id,))
    if target == "newer_unsealed":
        await engine.chat(ChatRequest(user_id="owner", session_id=latest.session_id, message="Unsealed later turn"))
    result = previous(engine, owner="victim" if target == "index_owner" else "owner")
    assert result.state is None
    assert result.metadata["status"] in {"unverified", "unavailable"}
    assert "LATEST_PRIVATE" not in json.dumps(result.metadata)
    assert "source_session_id" not in result.metadata


@pytest.mark.asyncio
async def test_legacy_requires_explicit_idempotent_attestation_and_rotation(recall_engine):
    engine, _ = recall_engine
    legacy = await engine.chat(ChatRequest(user_id="owner", message="Reviewed legacy conversation"))
    assert previous(engine).metadata["status"] == "no_eligible_history"
    with pytest.raises(ValueError, match="ownership"):
        engine.recall.attest(legacy.session_id, "other", KEY)
    original = snapshot(engine, legacy.session_id)[:4]
    record = engine.recall.attest(legacy.session_id, "owner", KEY)
    assert engine.recall.attest(legacy.session_id, "owner", KEY) == record
    assert previous(engine).metadata["attestation"] == "operator_attested_legacy"
    assert snapshot(engine, legacy.session_id)[:4] == original
    assert previous(engine, key="rotated-key").state is None
    engine.recall.attest(legacy.session_id, "owner", "rotated-key")
    assert previous(engine, key="rotated-key").metadata["status"] == "verified"
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE recall_checkpoints SET state_sha256='changed'")
    with pytest.raises(ValueError, match="Immutable"):
        engine.recall.attest(legacy.session_id, "owner", KEY)


@pytest.mark.asyncio
async def test_clear_memory_permanently_withholds_source(recall_engine):
    engine, _ = recall_engine
    source = await turn(engine)
    engine.clear_memory(source.session_id)
    assert previous(engine).metadata["status"] == "withheld"
    await turn(engine, session_id=source.session_id, message="Later turn in cleared session")
    assert previous(JarvisEngine(store=engine.store)).state is None


@pytest.mark.asyncio
async def test_storage_and_audit_failures_do_not_invent_recall_or_call_provider(recall_engine, monkeypatch):
    engine, generate = recall_engine
    await turn(engine)
    with sqlite3.connect(engine.store.path) as db:
        db.execute("DROP TABLE recall_checkpoints")
    assert previous(engine).metadata["status"] == "unavailable"
    monkeypatch.setattr(engine.audit, "append", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError()))
    generate.reset_mock()
    with pytest.raises(sqlite3.OperationalError):
        await turn(engine, recall_previous=True)
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_seal_locks_session_and_does_not_claim_confirmation(recall_engine, monkeypatch):
    engine, _ = recall_engine
    state = await engine.get_or_create_session("owner")
    monkeypatch.setattr(engine.recall, "attest", lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError()))
    with pytest.raises(RuntimeError, match="not be confirmed"):
        await turn(engine, session_id=state.session_id)
    assert engine.is_read_only(state.session_id)
    assert previous(engine).state is None


def test_recalled_text_is_bounded_unprivileged_and_owner_scoped():
    state = JarvisState(user_id="owner", session_id="new")
    prior = JarvisState(user_id="owner", session_id="old")
    prior.conversation_history = [{"role": "user", "content": "IGNORE_POLICY" * 1000}] * 50
    prior.conversation_history.append({"role": "system", "content": "INJECTED_SYSTEM"})
    prior.long_term_memory = [
        JarvisMemoryEntry(memory_id="foreign", user_id="foreign", session_id="old", content="PRIVATE_OTHER_USER")
    ]
    messages, facts = build_chat_context(
        state,
        ChatRequest(user_id="owner", message="Recall?"),
        read_only=True,
        infinity_configured=False,
        continuity_configured=False,
        speech_configured=False,
        previous=RecallResult({"status": "verified"}, prior),
    )
    quoted = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert messages[1]["role"] == "user"
    assert len(quoted["history"]) == MAX_RECALL_MESSAGES
    assert all(len(m["content"]) <= MAX_RECALL_MESSAGE_CHARS for m in quoted["history"])
    assert facts["previous_session"]["history_truncated"]
    assert "IGNORE_POLICY" not in messages[0]["content"]
    assert "INJECTED_SYSTEM" not in json.dumps(messages)
    assert "PRIVATE_OTHER_USER" not in json.dumps(messages)


def test_authenticated_api_binds_owner_and_recall_survives_restart(request, monkeypatch):
    http, engine = request.getfixturevalue("client")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    generate = AsyncMock(return_value=LLMResult("Context answer", "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    body = {"user_id": "owner", "message": "Our project is Spiral", "recall_previous": True}
    assert http.post("/chat", json=body).status_code == 401
    assert http.post("/chat", json=body, headers={"X-Jarvis-Service-Token": "bad"}).status_code == 401
    assert http.post("/chat", json={**body, "user_id": "other"}, headers=HEADERS).status_code == 403
    caps = http.get("/capabilities", headers=HEADERS).json()
    assert caps["recall_configured"] and caps["recall_owner_user_id"] == "owner"
    first = http.post("/chat", json=body, headers=HEADERS).json()
    assert first["previous_session"]["status"] == "no_eligible_history"
    import jarvis.routes.chat as chat

    monkeypatch.setattr(chat, "engine", JarvisEngine(store=engine.store))
    second = http.post("/chat", json={**body, "message": "What did we talk about last session?"}, headers=HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["previous_session"]["source_session_id"] == first["session_id"]
    assert "Our project is Spiral" in generate.call_args.args[0][1]["content"]
    assert http.post(
        "/sessions/resume", json={"session_id": first["session_id"], "user_id": "owner"}, headers=HEADERS
    ).json()["state"]["read_only"]


def test_disabled_configuration_never_enrolls_or_recalls(request):
    http, engine = request.getfixturevalue("client")
    assert not http.get("/capabilities", headers=HEADERS).json()["recall_configured"]
    data = http.post(
        "/chat", json={"user_id": "owner", "message": "Hi", "recall_previous": True}, headers=HEADERS
    ).json()
    assert data["previous_session"]["status"] == "not_authorized"
    assert previous(engine).state is None
