"""Fail-closed thresholds and helpers shared by policy lanes.

Thresholds are part of the Phase 1 contract:

* uncertainty >= 0.50
* stress > 0.80
* audit unavailable
* continuity auth unknown
* hash verification failure
* provider identity unavailable
* checkpoint integrity failure
"""

from __future__ import annotations

from typing import assert_never

from jarvis.governance.schemas import ContinuityAuthStatus, FailClosedReason, PolicyContext

UNCERTAINTY_FAIL_CLOSED_THRESHOLD = 0.50
STRESS_FAIL_CLOSED_THRESHOLD = 0.80


def uncertainty_from_confidence(confidence: float) -> float:
    """Map engine confidence in ``[0, 1]`` to uncertainty ``1 - confidence``."""

    bounded = min(1.0, max(0.0, confidence))
    return round(1.0 - bounded, 4)


def is_uncertainty_fail_closed(uncertainty: float) -> bool:
    return uncertainty >= UNCERTAINTY_FAIL_CLOSED_THRESHOLD


def is_stress_fail_closed(stress: float) -> bool:
    return stress > STRESS_FAIL_CLOSED_THRESHOLD


def is_audit_unavailable_fail_closed(audit_available: bool) -> bool:
    return not audit_available


def is_continuity_auth_unknown_fail_closed(status: ContinuityAuthStatus) -> bool:
    return status is ContinuityAuthStatus.UNKNOWN


def is_hash_verification_fail_closed(hash_verified: bool) -> bool:
    return not hash_verified


def is_provider_identity_fail_closed(provider_identity_available: bool) -> bool:
    return not provider_identity_available


def is_checkpoint_integrity_fail_closed(checkpoint_integrity_ok: bool) -> bool:
    return not checkpoint_integrity_ok


def fail_closed_message(reason: FailClosedReason) -> str:
    """Human-readable explanation for a fail-closed reason (facts only)."""

    match reason:
        case FailClosedReason.UNCERTAINTY:
            return f"uncertainty at or above {UNCERTAINTY_FAIL_CLOSED_THRESHOLD:.2f}"
        case FailClosedReason.STRESS:
            return f"stress above {STRESS_FAIL_CLOSED_THRESHOLD:.2f}"
        case FailClosedReason.AUDIT_UNAVAILABLE:
            return "audit subsystem unavailable"
        case FailClosedReason.CONTINUITY_AUTH_UNKNOWN:
            return "continuity memory auth unknown"
        case FailClosedReason.HASH_VERIFICATION_FAILURE:
            return "content or ledger hash verification failed"
        case FailClosedReason.PROVIDER_IDENTITY_UNAVAILABLE:
            return "provider identity unavailable"
        case FailClosedReason.CHECKPOINT_INTEGRITY_FAILURE:
            return "reviver checkpoint integrity failed"
        case _:
            assert_never(reason)


def fail_closed_reason_for_context(context: PolicyContext) -> FailClosedReason | None:
    """First fail-closed reason in policy-lane precedence order, if any.

    Order matches Safety → Continuity → Audit → Cost → Drift so a later
    phase can log the same reason the composer would surface.
    """

    if is_stress_fail_closed(context.stress):
        return FailClosedReason.STRESS
    if is_uncertainty_fail_closed(context.uncertainty):
        return FailClosedReason.UNCERTAINTY
    if is_continuity_auth_unknown_fail_closed(context.continuity_auth):
        return FailClosedReason.CONTINUITY_AUTH_UNKNOWN
    if is_audit_unavailable_fail_closed(context.audit_available):
        return FailClosedReason.AUDIT_UNAVAILABLE
    if is_provider_identity_fail_closed(context.provider_identity_available):
        return FailClosedReason.PROVIDER_IDENTITY_UNAVAILABLE
    if is_hash_verification_fail_closed(context.hash_verified):
        return FailClosedReason.HASH_VERIFICATION_FAILURE
    if is_checkpoint_integrity_fail_closed(context.checkpoint_integrity_ok):
        return FailClosedReason.CHECKPOINT_INTEGRITY_FAILURE
    return None
