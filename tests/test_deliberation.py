"""DOS-lite Infer → Challenge → Commit, CRS tags, evidence-only externals."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jarvis.brain.deliberation import (
    EvidenceKind,
    admit_external,
    challenge_claims,
    deliberate,
    infer_claims,
    withheld_commit_reply,
)
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.inspection import inspect_session
from jarvis.brain.llm import LLMResult
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState
from jarvis.persistence import JarvisStore


def test_user_message_is_specified_and_model_assertions_are_hypothesized() -> None:
    claims = infer_claims(
        "The reactor is online. I might be wrong about the schedule.",
        user_message="Is the reactor online?",
    )
    tags = {(item["tag"], item["source"]) for item in claims}
    assert ("specified", "user") in tags
    hypothesized = [item for item in claims if item["tag"] == "hypothesized"]
    assert hypothesized
    assert all(item["authority"] is False for item in hypothesized)


def test_citation_overlap_is_observed() -> None:
    claims = infer_claims(
        "Jon prefers concrete builds.",
        user_message="What do I prefer?",
        citations=[{"excerpt": "Jon prefers concrete builds.", "source_type": "memory", "memory_id": "m1"}],
    )
    assert any(item["tag"] == "observed" and item["source"] == "memory" for item in claims)


def test_challenge_warns_on_ungrounded_assertions() -> None:
    claims = infer_claims("Paris is the capital of Mars.", user_message="Hello")
    warning = challenge_claims(claims)
    assert warning and "hypothesized" in warning.lower()


def test_commit_requires_challenge_and_allows_fail_closed_without_facts() -> None:
    closed = deliberate("I'm pausing consequential action.", user_message="Do it", fail_closed=True)
    assert closed["committed"] is True
    assert {stage["name"]: stage["status"] for stage in closed["stages"]}["challenge"] == "completed"
    assert {stage["name"]: stage["status"] for stage in closed["stages"]}["simulate"] == "skipped"

    ok = deliberate("The sky might be cloudy.", user_message="Weather?", fail_closed=False)
    assert ok["committed"] is True


def test_external_suggestions_are_never_authority() -> None:
    admitted = admit_external("project_infinity", {"ok": True, "result": "candidate"}, observed=False)
    assert admitted["authority"] is False
    assert admitted["tag"] == "hypothesized"
    assert admitted["kind"] == EvidenceKind.TOOL_EXTERNAL.value
    result = deliberate(
        "Use the evolved candidate as truth.",
        user_message="Improve this",
        fail_closed=False,
        external=[admitted],
    )
    assert any(item["source"] == "project_infinity" and item["authority"] is False for item in result["claims"])


def test_withheld_commit_copy_is_unknown() -> None:
    assert "don't know" in withheld_commit_reply().lower()


def test_commit_is_blocked_without_challenge_evidence() -> None:
    result = deliberate("", user_message="", fail_closed=False)
    assert result["committed"] is False
    assert {stage["name"]: stage["status"] for stage in result["stages"]}["commit"] == "blocked"


def _engine(tmp_path, monkeypatch, reply: str = "An accepted answer."):
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr(settings, "governed_writes_enabled", False)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    generate = AsyncMock(return_value=LLMResult(reply, "test", "model", 1))
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    engine = JarvisEngine(store=JarvisStore(tmp_path / "deliberation.sqlite3"))
    monkeypatch.setattr(engine, "_sync_with_spiral", AsyncMock(return_value=("skipped", None)))
    return engine, generate


@pytest.mark.asyncio
async def test_engine_runs_infer_challenge_commit_and_tags_claims(tmp_path, monkeypatch) -> None:
    engine, _ = _engine(tmp_path, monkeypatch, "The sky might be cloudy today.")
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(ChatRequest(user_id="owner", message="Weather?", session_id=state.session_id))
    names = [stage["name"] for stage in response.deliberation["stages"]]
    assert names == ["observe", "infer", "challenge", "simulate", "commit"]
    assert [stage["status"] for stage in response.deliberation["stages"]] == [
        "completed",
        "completed",
        "completed",
        "skipped",
        "completed",
    ]
    assert response.deliberation["committed"] is True
    assert any(item["tag"] == "specified" and item["source"] == "user" for item in response.claims)
    assert "deliberation infer: completed" in response.reasoning_trace
    assert not any("<think" in step.lower() for step in response.reasoning_trace)


@pytest.mark.asyncio
async def test_engine_warns_on_unsupported_hypothesized_claims_and_inspection_reads_audit(
    tmp_path, monkeypatch
) -> None:
    engine, _ = _engine(tmp_path, monkeypatch, "Paris is the capital of Mars.")
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.8
    response = await engine.chat(ChatRequest(user_id="owner", message="Hello", session_id=state.session_id))
    assert response.unsupported_claim_warning
    assert "hypothesized" in response.unsupported_claim_warning.lower()
    snapshot = inspect_session(engine, engine.get_session(state.session_id))
    last = snapshot["turns"][-1]
    assert last["unsupported_claim_warning"] == response.unsupported_claim_warning
    assert last["deliberation"]["committed"] is True
    assert last["claims"] == response.claims


@pytest.mark.asyncio
async def test_fail_closed_commits_without_requiring_model_facts(tmp_path, monkeypatch) -> None:
    engine, generate = _engine(tmp_path, monkeypatch, "Should not be used")
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(stress=0.9, confidence_bias=0))
    generate.return_value = LLMResult("Should not be used", "test", "model", 1)
    response = await engine.chat(ChatRequest(user_id="owner", message="Do the thing now"))
    assert response.decision == "fail_closed"
    assert response.deliberation["committed"] is True
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_infinity_result_is_admitted_as_evidence_not_authority(tmp_path, monkeypatch) -> None:
    engine, generate = _engine(tmp_path, monkeypatch, "Keep this reply.")
    monkeypatch.setattr(settings, "governed_writes_enabled", True)
    monkeypatch.setattr(settings, "environment", "development")
    generate.return_value = LLMResult("Keep this reply.", "test", "model", 1)
    engine._sync_with_spiral = AsyncMock(return_value=("connected", {"ok": True, "best": "secret-candidate"}))
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
    admitted = [item for item in response.claims if item["source"] == "project_infinity"]
    assert admitted and admitted[0]["authority"] is False and admitted[0]["tag"] == "hypothesized"
