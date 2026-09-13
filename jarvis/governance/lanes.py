"""Initial PolicyLane stubs — real, testable, and precedence-aware.

Each stub delegates threshold checks to :mod:`jarvis.governance.fail_closed`
so later phases can replace a lane's internals without changing composer
or fail-closed contracts.
"""

from __future__ import annotations

from jarvis.governance.fail_closed import (
    fail_closed_message,
    is_audit_unavailable_fail_closed,
    is_checkpoint_integrity_fail_closed,
    is_continuity_auth_unknown_fail_closed,
    is_hash_verification_fail_closed,
    is_provider_identity_fail_closed,
    is_stress_fail_closed,
    is_uncertainty_fail_closed,
)
from jarvis.governance.policy import PolicyLane, compose_policy_lanes
from jarvis.governance.schemas import (
    CompositePolicyDecision,
    ContinuityAuthStatus,
    FailClosedReason,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
)


class _LaneHelpers:
    name: PolicyLaneName

    def allow(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            lane=self.name,
            effect=PolicyEffect.ALLOW,
            blocking=False,
            reason=reason,
        )

    def fail_closed(self, code: FailClosedReason, reason: str | None = None) -> PolicyDecision:
        return PolicyDecision(
            lane=self.name,
            effect=PolicyEffect.FAIL_CLOSED,
            blocking=True,
            reason=reason or fail_closed_message(code),
            fail_closed_reason=code,
        )

    def read_only(self, reason: str) -> PolicyDecision:
        return PolicyDecision(
            lane=self.name,
            effect=PolicyEffect.READ_ONLY,
            blocking=True,
            reason=reason,
        )


class SafetyPolicyLane(_LaneHelpers):
    """Blocks on high stress or high uncertainty. Cannot be overridden later."""

    name = PolicyLaneName.SAFETY

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if is_stress_fail_closed(context.stress):
            return self.fail_closed(FailClosedReason.STRESS)
        if is_uncertainty_fail_closed(context.uncertainty):
            return self.fail_closed(FailClosedReason.UNCERTAINTY)
        return self.allow("safety checks passed")


class ContinuityPolicyLane(_LaneHelpers):
    """Blocks when Continuity (persistence-memory) auth is unknown."""

    name = PolicyLaneName.CONTINUITY

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if is_continuity_auth_unknown_fail_closed(context.continuity_auth):
            return self.fail_closed(FailClosedReason.CONTINUITY_AUTH_UNKNOWN)
        if context.continuity_auth is ContinuityAuthStatus.DENIED:
            return self.read_only("continuity memory auth denied; read-only")
        return self.allow("continuity auth known")


class AuditPolicyLane(_LaneHelpers):
    """Blocks when the Audit Trace / Audit Ledger path is unavailable."""

    name = PolicyLaneName.AUDIT

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if is_audit_unavailable_fail_closed(context.audit_available):
            return self.fail_closed(FailClosedReason.AUDIT_UNAVAILABLE)
        return self.allow("audit available")


class CostPolicyLane(_LaneHelpers):
    """Cost/provider lane — fail-closed without a known provider identity."""

    name = PolicyLaneName.COST

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if is_provider_identity_fail_closed(context.provider_identity_available):
            return self.fail_closed(FailClosedReason.PROVIDER_IDENTITY_UNAVAILABLE)
        return self.allow("provider identity available")


class DriftPolicyLane(_LaneHelpers):
    """Drift/checkpoint lane — hash and Reviver integrity must hold."""

    name = PolicyLaneName.DRIFT

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if is_hash_verification_fail_closed(context.hash_verified):
            return self.fail_closed(FailClosedReason.HASH_VERIFICATION_FAILURE)
        if is_checkpoint_integrity_fail_closed(context.checkpoint_integrity_ok):
            return self.fail_closed(FailClosedReason.CHECKPOINT_INTEGRITY_FAILURE)
        return self.allow("hash and checkpoint integrity ok")


class SideEffectPolicyLane(_LaneHelpers):
    """Blocks side effects unless explicitly authorized (read-only)."""

    name = PolicyLaneName.SIDE_EFFECT

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if context.side_effects_requested and not context.side_effects_authorized:
            return self.read_only("side effects requested but not authorized")
        return self.allow("no unauthorized side effects")


def default_policy_lanes() -> list[PolicyLane]:
    """Canonical Phase 1 lane set in precedence order."""

    return [
        SafetyPolicyLane(),
        ContinuityPolicyLane(),
        AuditPolicyLane(),
        CostPolicyLane(),
        DriftPolicyLane(),
        SideEffectPolicyLane(),
    ]


def evaluate_policies(
    context: PolicyContext,
    lanes: list[PolicyLane] | None = None,
) -> CompositePolicyDecision:
    """Run the default (or supplied) lanes through the composer."""

    selected = default_policy_lanes() if lanes is None else lanes
    return compose_policy_lanes(selected, context)
