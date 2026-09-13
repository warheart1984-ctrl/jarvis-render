"""Tests for governance schemas, secret omission, and Continuity stubs."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jarvis.governance.adapters import (
    make_spiral_turn_id,
    policy_context_from_state,
    spiral_turn_from_state,
)
from jarvis.governance.continuity import UnimplementedContinuityClient
from jarvis.governance.hashing import content_hash
from jarvis.governance.lanes import evaluate_policies
from jarvis.governance.schemas import (
    AuditLedgerEventType,
    ContinuityHealth,
    ContinuityRecallQuery,
    EvidenceRecord,
    FailClosedReason,
    MemoryProposal,
    OutcomeStatus,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
    ProviderMetadata,
    PublicAuditTrace,
    ReviverCheckpoint,
    SideEffectStatus,
    SpiralTurn,
    StressSignals,
    TurnDecision,
    VerificationStatus,
    build_audit_ledger_event,
    to_public_audit_trace,
)
from jarvis.governance.secrets import is_secret_key, omit_secrets
from jarvis.models.jarvis_types import EmotionState, JarvisMemoryEntry, JarvisState, SpiralPhase
from jarvis.models.spiral_types import IntentMode


def test_evidence_record_omits_secret_metadata() -> None:
    record = EvidenceRecord(
        evidence_id="e1",
        source="memory",
        summary="user prefers concrete builds",
        metadata={"api_key": "sk-secret", "fact": "prefers_concrete_builds", "authorization": "Bearer abc"},
    )
    assert "api_key" not in record.metadata
    assert "authorization" not in record.metadata
    assert record.metadata["fact"] == "prefers_concrete_builds"


def test_provider_metadata_rejects_api_key() -> None:
    with pytest.raises(ValidationError):
        ProviderMetadata(provider="openai", model="gpt-4", api_key="sk-test")  # type: ignore[call-arg]


def test_public_audit_trace_rejects_hidden_prompt_field() -> None:
    with pytest.raises(ValidationError):
        PublicAuditTrace(
            turn_id="t1",
            session_id="s1",
            input_hash="aa",
            output_hash="bb",
            confidence=0.8,
            uncertainty=0.2,
            stress_signals=StressSignals(),
            decision=TurnDecision.ANSWER,
            side_effect_status=SideEffectStatus.NONE,
            outcome_status=OutcomeStatus.OK,
            hidden_prompt="do not expose",  # type: ignore[call-arg]
        )


def test_public_trace_view_omits_secrets_and_private_reasoning() -> None:
    turn = SpiralTurn(
        turn_id="s1:turn:1",
        session_id="s1",
        input_hash=content_hash({"message": "hello"}),
        output_hash=content_hash({"reply": "hi"}),
        evidence=[
            EvidenceRecord(
                evidence_id="e1",
                source="rule",
                summary="safety allow",
                metadata={"api_key": "sk-leak", "rule_id": "safety.allow"},
            )
        ],
        confidence=0.8,
        uncertainty=0.2,
        decision=TurnDecision.ANSWER,
        facts=["user asked a greeting"],
        rules_applied=["safety.allow"],
    )
    view = to_public_audit_trace(turn)
    blob = json.dumps(view.model_dump(mode="json"))
    assert "sk-leak" not in blob
    assert "api_key" not in blob
    assert "hidden_prompt" not in blob
    assert "private_reasoning" not in blob
    assert view.facts == ["user asked a greeting"]
    assert view.evidence[0].metadata == {"rule_id": "safety.allow"}


def test_omit_secrets_nested() -> None:
    cleaned = omit_secrets(
        {
            "ok": 1,
            "headers": {"Authorization": "Bearer z", "x-request-id": "r1"},
            "nested": {"private_reasoning": "cot", "note": "fine"},
        }
    )
    assert cleaned == {"ok": 1, "headers": {"x-request-id": "r1"}, "nested": {"note": "fine"}}
    assert is_secret_key("api_key")
    assert is_secret_key("hidden_prompt")
    assert not is_secret_key("rule_id")


def test_audit_ledger_event_strips_secrets_and_chains() -> None:
    first = build_audit_ledger_event(
        event_id="evt-1",
        turn_id="s1:turn:1",
        session_id="s1",
        event_type=AuditLedgerEventType.TURN_RECORDED,
        payload={"fact": "answered", "authorization": "Bearer secret", "api_key": "sk-x"},
        previous_event_hash=None,
    )
    assert first.normalized_payload == {"fact": "answered"}
    assert first.verify_integrity()
    assert first.previous_event_hash is None

    second = build_audit_ledger_event(
        event_id="evt-2",
        turn_id="s1:turn:1",
        session_id="s1",
        event_type=AuditLedgerEventType.POLICY_EVALUATED,
        payload={"lane": "safety"},
        previous_event_hash=first.current_event_hash,
    )
    assert second.verify_integrity()
    assert second.previous_event_hash == first.current_event_hash
    assert second.current_event_hash != first.current_event_hash


def test_reviver_checkpoint_rejects_api_keys_and_strips_task_secrets() -> None:
    with pytest.raises(ValidationError):
        ReviverCheckpoint(
            checkpoint_id="c1",
            session_id="s1",
            audit_ledger_hash="abc",
            provider_config_ref="provider:mock",
            api_key="sk-nope",  # type: ignore[call-arg]
        )

    checkpoint = ReviverCheckpoint(
        checkpoint_id="c1",
        session_id="s1",
        last_verified_turn_id="s1:turn:3",
        audit_ledger_hash="abc",
        continuity_cursor="cur-1",
        continuity_version="v3",
        serialized_state_refs=["state:s1:3"],
        pending_task_state={"step": "sync", "token": "abc123"},
        provider_config_ref="provider:mock#cfg-1",
        verification_status=VerificationStatus.UNVERIFIED,
    )
    assert "token" not in checkpoint.pending_task_state
    assert checkpoint.pending_task_state["step"] == "sync"
    assert checkpoint.is_resume_ready is False

    verified = checkpoint.model_copy(
        update={"verification_status": VerificationStatus.VERIFIED, "verified_at": checkpoint.created_at}
    )
    assert verified.is_resume_ready is True


def test_policy_decision_rejects_blocking_allow() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(lane=PolicyLaneName.SAFETY, effect=PolicyEffect.ALLOW, blocking=True)


@pytest.mark.asyncio
async def test_unimplemented_continuity_client_is_not_sqlite_authority() -> None:
    client = UnimplementedContinuityClient()
    health = await client.health()
    assert isinstance(health, ContinuityHealth)
    assert health.sqlite_is_authority is False
    assert health.service == "persistence-memory"

    recall = await client.recall(ContinuityRecallQuery(session_id="s1", query="prefs"))
    assert recall.memory_ids == []
    proposal = await client.propose_memory(
        MemoryProposal(session_id="s1", turn_id="s1:turn:1", content_hash="aa", summary="note")
    )
    assert proposal.accepted is False
    assert await client.retrieve("mem-1") is None
    assert await client.list_conflicts("s1") == []


def test_spiral_turn_from_existing_jarvis_state() -> None:
    state = JarvisState(
        session_id="jarvis-session-test",
        user_id="jon",
        phase=SpiralPhase.RESPOND,
        intent=IntentMode.TRANSFORM,
        confidence=0.8,
        turn_count=2,
        emotion=EmotionState(stress=0.1, urgency=0.2, inferred_emotion="calm"),
        long_term_memory=[
            JarvisMemoryEntry(
                memory_id="mem-1",
                user_id="jon",
                session_id="jarvis-session-test",
                content="prefers concrete builds",
            )
        ],
    )
    context = policy_context_from_state(state)
    assert context.session_id == state.session_id
    assert context.uncertainty == pytest.approx(0.2)
    policy = evaluate_policies(context)
    turn = spiral_turn_from_state(
        state,
        input_payload={"message": "continue"},
        output_payload={"reply": "ok"},
        policy=policy,
        facts=["user asked to continue"],
        rules_applied=["safety.allow"],
    )
    assert turn.turn_id == make_spiral_turn_id(state.session_id, 2)
    assert turn.retrieved_memory_ids == ["mem-1"]
    assert turn.phase is SpiralPhase.RESPOND
    assert turn.intent is IntentMode.TRANSFORM
    assert turn.decision is TurnDecision.ANSWER
    assert turn.input_hash == content_hash({"message": "continue"})
    public = turn.to_public_audit_trace()
    assert public.session_id == state.session_id


def test_spiral_turn_fail_closed_from_high_stress_state() -> None:
    state = JarvisState(
        session_id="s-stress",
        user_id="jon",
        confidence=0.7,
        emotion=EmotionState(stress=0.9),
    )
    policy = evaluate_policies(policy_context_from_state(state, turn_id="s-stress:turn:0"))
    turn = spiral_turn_from_state(
        state,
        turn_id="s-stress:turn:0",
        input_payload={"message": "broken"},
        output_payload={"reply": ""},
        policy=policy,
    )
    assert turn.decision is TurnDecision.FAIL_CLOSED
    assert turn.fail_closed_reason is FailClosedReason.STRESS
    assert turn.outcome_status is OutcomeStatus.FAIL_CLOSED
    assert turn.side_effect_status is SideEffectStatus.BLOCKED
