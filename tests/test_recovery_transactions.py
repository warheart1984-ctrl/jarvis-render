from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.models.jarvis_types import ChatRequest, SpiralTurn
from jarvis.persistence import AuditLedger, JarvisStore, ReviverLedger


@pytest.mark.asyncio
async def test_restart_recovers_verified_checkpoint_as_read_only(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.sqlite3"
    first = JarvisEngine(store=JarvisStore(path))
    response = await first.chat(ChatRequest(user_id="u1", message="Remember this stable fact."))

    restarted = JarvisEngine(store=JarvisStore(path))
    state = await restarted.get_or_create_session("u1", response.session_id)

    assert state.session_id == response.session_id
    assert restarted.is_read_only(response.session_id)
    assert restarted.lock_reason(response.session_id).value == "recovery"
    assert state.turn_count == 1
    with pytest.raises(ValueError, match="reason=recovery"):
        await restarted.chat(ChatRequest(user_id="u1", session_id=response.session_id, message="Continue"))


@pytest.mark.asyncio
async def test_conflict_and_verification_locks_use_distinct_reason_codes(tmp_path: Path) -> None:
    path = tmp_path / "locks.sqlite3"
    engine = JarvisEngine(store=JarvisStore(path))
    response = await engine.chat(ChatRequest(user_id="u1", message="start"))
    engine.set_read_only(response.session_id, "conflict")
    assert engine.lock_reason(response.session_id).value == "conflict"
    with pytest.raises(ValueError, match="reason=conflict"):
        await engine.chat(ChatRequest(user_id="u1", session_id=response.session_id, message="Continue"))
    other = await engine.chat(ChatRequest(user_id="u1", message="another session"))
    engine.set_read_only(other.session_id, "verification")
    summary = engine.get_state_summary(other.session_id)
    assert summary["lock_reason"] == "verification"
    assert summary["recovered"] is False
    recovered = engine.get_state_summary(response.session_id)
    assert recovered["lock_reason"] == "conflict"
    assert recovered["recovered"] is False
    engine.authorize_session(response.session_id)
    assert engine.is_read_only(response.session_id) is False
    engine.authorize_session(other.session_id)
    assert engine.is_read_only(other.session_id) is True


@pytest.mark.asyncio
async def test_restart_refuses_tampered_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.sqlite3"
    first = JarvisEngine(store=JarvisStore(path))
    response = await first.chat(ChatRequest(user_id="u1", message="Create a checkpoint."))
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE audit_events SET payload_json=? WHERE session_id=?", ('{"tampered":true}', response.session_id)
        )

    restarted = JarvisEngine(store=JarvisStore(path))
    with pytest.raises(ValueError, match="could not be verified"):
        await restarted.get_or_create_session("u1", response.session_id)
    assert restarted.get_session(response.session_id) is None


def test_turn_bundle_rolls_back_on_duplicate_turn(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.sqlite3"
    store = JarvisStore(path)
    audit = AuditLedger(path)
    turn = SpiralTurn(
        turn_id="same",
        session_id="s",
        timestamp=datetime.now(timezone.utc),
        decision="answer",
        uncertainty=0.1,
        stress=0,
        content="ok",
        content_sha256=store.content_hash("ok"),
    )
    event = {"event_id": "event-1", "event_type": "turn", "timestamp": turn.timestamp.isoformat(), "payload": {}}
    store.save_turn_bundle(turn, event)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_turn_bundle(turn, {**event, "event_id": "event-2"})
    assert len(store.load_session_turns("s")) == 1
    assert len(audit.list("s")) == 1


def test_reviver_rejects_checkpoint_for_wrong_audit_event(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.sqlite3"
    audit = AuditLedger(path)
    audit.append("e1", "s", "turn", {})
    reviver = ReviverLedger(path)
    reviver.save("c1", "s", "missing-turn", "not-a-real-hash", {"session_id": "s"}, verified=True)
    assert reviver.recover("s", audit) is None
