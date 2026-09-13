"""Tests for policy composition, precedence, and lane stubs."""

from __future__ import annotations

from jarvis.governance.lanes import (
    AuditPolicyLane,
    CostPolicyLane,
    DriftPolicyLane,
    SafetyPolicyLane,
    SideEffectPolicyLane,
    default_policy_lanes,
    evaluate_policies,
)
from jarvis.governance.policy import LANE_PRECEDENCE, compose_policy_lanes
from jarvis.governance.schemas import (
    ContinuityAuthStatus,
    FailClosedReason,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
    TurnDecision,
)


def _context(**overrides: object) -> PolicyContext:
    payload = {
        "session_id": "sess",
        "turn_id": "sess:turn:1",
    }
    payload.update(overrides)
    return PolicyContext(**payload)  # type: ignore[arg-type]


class _AllowLane:
    def __init__(self, name: PolicyLaneName) -> None:
        self.name = name

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(
            lane=self.name,
            effect=PolicyEffect.ALLOW,
            blocking=False,
            reason=f"{self.name.value} allow",
        )


class _BlockLane:
    def __init__(self, name: PolicyLaneName, effect: PolicyEffect, reason_code: FailClosedReason | None = None) -> None:
        self.name = name
        self._effect = effect
        self._reason_code = reason_code

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(
            lane=self.name,
            effect=self._effect,
            blocking=True,
            reason=f"{self.name.value} block",
            fail_closed_reason=self._reason_code,
        )


def test_lane_precedence_order() -> None:
    assert LANE_PRECEDENCE == (
        PolicyLaneName.SAFETY,
        PolicyLaneName.CONTINUITY,
        PolicyLaneName.AUDIT,
        PolicyLaneName.COST,
        PolicyLaneName.DRIFT,
        PolicyLaneName.SIDE_EFFECT,
    )


def test_default_lanes_match_precedence() -> None:
    names = [lane.name for lane in default_policy_lanes()]
    assert names == list(LANE_PRECEDENCE)


def test_all_lanes_allow_when_context_is_healthy() -> None:
    result = evaluate_policies(_context())
    assert result.effect is PolicyEffect.ALLOW
    assert result.blocking is False
    assert result.fail_closed_reason is None
    assert result.suggested_turn_decision is TurnDecision.ANSWER
    assert [item.lane for item in result.lane_decisions] == list(LANE_PRECEDENCE)


def test_safety_block_is_not_overridden_by_later_allow_lanes() -> None:
    lanes = [
        _AllowLane(PolicyLaneName.COST),
        _BlockLane(PolicyLaneName.SAFETY, PolicyEffect.FAIL_CLOSED, FailClosedReason.STRESS),
        _AllowLane(PolicyLaneName.SIDE_EFFECT),
        _AllowLane(PolicyLaneName.DRIFT),
    ]
    result = compose_policy_lanes(lanes, _context(stress=0.9))
    assert result.effect is PolicyEffect.FAIL_CLOSED
    assert result.blocking is True
    assert result.blocking_lane is PolicyLaneName.SAFETY
    assert result.fail_closed_reason is FailClosedReason.STRESS
    assert [item.lane for item in result.lane_decisions][0] is PolicyLaneName.SAFETY


def test_later_lane_cannot_override_earlier_block_with_its_own_block() -> None:
    lanes = [
        _BlockLane(PolicyLaneName.SAFETY, PolicyEffect.FAIL_CLOSED, FailClosedReason.UNCERTAINTY),
        _BlockLane(PolicyLaneName.COST, PolicyEffect.READ_ONLY),
    ]
    result = compose_policy_lanes(lanes, _context())
    assert result.effect is PolicyEffect.FAIL_CLOSED
    assert result.blocking_lane is PolicyLaneName.SAFETY
    assert result.fail_closed_reason is FailClosedReason.UNCERTAINTY


def test_later_block_applies_when_safety_allows() -> None:
    result = evaluate_policies(_context(continuity_auth=ContinuityAuthStatus.UNKNOWN))
    assert result.effect is PolicyEffect.FAIL_CLOSED
    assert result.blocking_lane is PolicyLaneName.CONTINUITY
    assert result.fail_closed_reason is FailClosedReason.CONTINUITY_AUTH_UNKNOWN


def test_safety_lane_fail_closed_on_high_stress() -> None:
    decision = SafetyPolicyLane().evaluate(_context(stress=0.81))
    assert decision.blocking is True
    assert decision.effect is PolicyEffect.FAIL_CLOSED
    assert decision.fail_closed_reason is FailClosedReason.STRESS


def test_safety_lane_fail_closed_on_low_confidence_uncertainty() -> None:
    decision = SafetyPolicyLane().evaluate(_context(uncertainty=0.50))
    assert decision.fail_closed_reason is FailClosedReason.UNCERTAINTY


def test_side_effect_lane_read_only_when_unauthorized() -> None:
    decision = SideEffectPolicyLane().evaluate(_context(side_effects_requested=True, side_effects_authorized=False))
    assert decision.effect is PolicyEffect.READ_ONLY
    assert decision.blocking is True


def test_cost_lane_fail_closed_without_provider_identity() -> None:
    decision = CostPolicyLane().evaluate(_context(provider_identity_available=False))
    assert decision.fail_closed_reason is FailClosedReason.PROVIDER_IDENTITY_UNAVAILABLE


def test_audit_and_drift_lanes_fail_closed() -> None:
    assert (
        AuditPolicyLane().evaluate(_context(audit_available=False)).fail_closed_reason
        is FailClosedReason.AUDIT_UNAVAILABLE
    )
    assert (
        DriftPolicyLane().evaluate(_context(hash_verified=False)).fail_closed_reason
        is FailClosedReason.HASH_VERIFICATION_FAILURE
    )
    assert (
        DriftPolicyLane().evaluate(_context(checkpoint_integrity_ok=False)).fail_closed_reason
        is FailClosedReason.CHECKPOINT_INTEGRITY_FAILURE
    )


def test_evaluate_policies_records_all_lanes_after_safety_block() -> None:
    result = evaluate_policies(_context(stress=0.95))
    assert result.blocking_lane is PolicyLaneName.SAFETY
    assert len(result.lane_decisions) == len(LANE_PRECEDENCE)
    later = [item for item in result.lane_decisions if item.lane is not PolicyLaneName.SAFETY]
    assert later
    assert all(item.effect is PolicyEffect.ALLOW for item in later)
