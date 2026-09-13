"""Bridges from existing Jarvis session types into governance contracts.

Phase 1 does not wire the chat engine; these helpers are the extension
points later phases should call so SpiralTurn / PolicyContext stay
aligned with :class:`~jarvis.models.jarvis_types.JarvisState`.
"""

from __future__ import annotations

from typing import Any, assert_never

from jarvis.governance.fail_closed import uncertainty_from_confidence
from jarvis.governance.hashing import content_hash
from jarvis.governance.schemas import (
    CompositePolicyDecision,
    EvidenceRecord,
    OutcomeStatus,
    PolicyContext,
    PolicyEffect,
    ProviderMetadata,
    SpiralTurn,
    StressSignals,
    TurnDecision,
)
from jarvis.models.jarvis_types import JarvisState


def make_spiral_turn_id(session_id: str, turn_index: int) -> str:
    """Stable turn id derived from the existing session + turn count."""

    if turn_index < 0:
        raise ValueError("turn_index must be >= 0")
    return f"{session_id}:turn:{turn_index}"


def policy_context_from_state(
    state: JarvisState,
    *,
    turn_id: str | None = None,
    **overrides: Any,
) -> PolicyContext:
    """Build a :class:`PolicyContext` from live Jarvis session signals."""

    resolved_turn_id = turn_id or make_spiral_turn_id(state.session_id, state.turn_count)
    payload: dict[str, Any] = {
        "session_id": state.session_id,
        "turn_id": resolved_turn_id,
        "uncertainty": uncertainty_from_confidence(state.confidence),
        "stress": state.emotion.stress,
    }
    payload.update(overrides)
    return PolicyContext(**payload)


def spiral_turn_from_state(
    state: JarvisState,
    *,
    input_payload: Any,
    output_payload: Any,
    policy: CompositePolicyDecision,
    turn_id: str | None = None,
    retrieved_memory_ids: list[str] | None = None,
    evidence: list[EvidenceRecord] | None = None,
    provider: ProviderMetadata | None = None,
    facts: list[str] | None = None,
    rules_applied: list[str] | None = None,
    error_status: str | None = None,
    decision: TurnDecision | None = None,
) -> SpiralTurn:
    """Construct a governed turn record from JarvisState + policy output."""

    resolved_turn_id = turn_id or make_spiral_turn_id(state.session_id, state.turn_count)
    uncertainty = uncertainty_from_confidence(state.confidence)
    resolved_decision = decision or policy.suggested_turn_decision

    if error_status:
        outcome = OutcomeStatus.ERROR
    elif resolved_decision is TurnDecision.FAIL_CLOSED:
        outcome = OutcomeStatus.FAIL_CLOSED
    elif resolved_decision is TurnDecision.ABSTAIN:
        outcome = OutcomeStatus.ABSTAINED
    elif resolved_decision is TurnDecision.ANSWER:
        outcome = OutcomeStatus.OK
    elif resolved_decision is TurnDecision.PLAN:
        outcome = OutcomeStatus.OK
    else:
        assert_never(resolved_decision)

    memory_ids = retrieved_memory_ids
    if memory_ids is None:
        memory_ids = [entry.memory_id for entry in state.long_term_memory]

    return SpiralTurn(
        turn_id=resolved_turn_id,
        session_id=state.session_id,
        input_hash=content_hash(input_payload),
        output_hash=content_hash(output_payload),
        retrieved_memory_ids=memory_ids,
        evidence=evidence or [],
        confidence=state.confidence,
        uncertainty=uncertainty,
        stress_signals=StressSignals.from_emotion(state.emotion),
        policy_lanes_evaluated=[item.lane for item in policy.lane_decisions],
        provider=provider,
        decision=resolved_decision,
        fail_closed_reason=policy.fail_closed_reason if policy.effect is PolicyEffect.FAIL_CLOSED else None,
        side_effect_status=policy.side_effect_status,
        outcome_status=outcome,
        error_status=error_status,
        facts=facts or [],
        rules_applied=rules_applied or [],
        phase=state.phase,
        intent=state.intent,
    )
