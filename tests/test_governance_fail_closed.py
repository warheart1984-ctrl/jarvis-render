"""Tests for fail-closed thresholds and helpers."""

from __future__ import annotations

from jarvis.governance.fail_closed import (
    STRESS_FAIL_CLOSED_THRESHOLD,
    UNCERTAINTY_FAIL_CLOSED_THRESHOLD,
    fail_closed_message,
    fail_closed_reason_for_context,
    is_audit_unavailable_fail_closed,
    is_checkpoint_integrity_fail_closed,
    is_continuity_auth_unknown_fail_closed,
    is_hash_verification_fail_closed,
    is_provider_identity_fail_closed,
    is_stress_fail_closed,
    is_uncertainty_fail_closed,
    uncertainty_from_confidence,
)
from jarvis.governance.schemas import ContinuityAuthStatus, FailClosedReason, PolicyContext


def _context(**overrides: object) -> PolicyContext:
    payload = {
        "session_id": "sess",
        "turn_id": "sess:turn:1",
    }
    payload.update(overrides)
    return PolicyContext(**payload)  # type: ignore[arg-type]


def test_uncertainty_threshold_is_inclusive() -> None:
    assert UNCERTAINTY_FAIL_CLOSED_THRESHOLD == 0.50
    assert not is_uncertainty_fail_closed(0.49)
    assert is_uncertainty_fail_closed(0.50)
    assert is_uncertainty_fail_closed(0.99)


def test_stress_threshold_is_exclusive() -> None:
    assert STRESS_FAIL_CLOSED_THRESHOLD == 0.80
    assert not is_stress_fail_closed(0.80)
    assert is_stress_fail_closed(0.81)


def test_low_confidence_maps_to_fail_closed_uncertainty() -> None:
    uncertainty = uncertainty_from_confidence(0.50)
    assert uncertainty == 0.50
    assert is_uncertainty_fail_closed(uncertainty)
    assert not is_uncertainty_fail_closed(uncertainty_from_confidence(0.51))


def test_remaining_fail_closed_helpers() -> None:
    assert is_audit_unavailable_fail_closed(False)
    assert not is_audit_unavailable_fail_closed(True)
    assert is_continuity_auth_unknown_fail_closed(ContinuityAuthStatus.UNKNOWN)
    assert not is_continuity_auth_unknown_fail_closed(ContinuityAuthStatus.KNOWN)
    assert is_hash_verification_fail_closed(False)
    assert is_provider_identity_fail_closed(False)
    assert is_checkpoint_integrity_fail_closed(False)


def test_fail_closed_reason_precedence_prefers_safety_stress() -> None:
    context = _context(
        stress=0.85,
        uncertainty=0.9,
        continuity_auth=ContinuityAuthStatus.UNKNOWN,
        audit_available=False,
    )
    assert fail_closed_reason_for_context(context) is FailClosedReason.STRESS


def test_fail_closed_reason_for_each_signal() -> None:
    assert fail_closed_reason_for_context(_context(uncertainty=0.5)) is FailClosedReason.UNCERTAINTY
    assert (
        fail_closed_reason_for_context(_context(continuity_auth=ContinuityAuthStatus.UNKNOWN))
        is FailClosedReason.CONTINUITY_AUTH_UNKNOWN
    )
    assert fail_closed_reason_for_context(_context(audit_available=False)) is FailClosedReason.AUDIT_UNAVAILABLE
    assert (
        fail_closed_reason_for_context(_context(provider_identity_available=False))
        is FailClosedReason.PROVIDER_IDENTITY_UNAVAILABLE
    )
    assert fail_closed_reason_for_context(_context(hash_verified=False)) is FailClosedReason.HASH_VERIFICATION_FAILURE
    assert (
        fail_closed_reason_for_context(_context(checkpoint_integrity_ok=False))
        is FailClosedReason.CHECKPOINT_INTEGRITY_FAILURE
    )
    assert fail_closed_reason_for_context(_context()) is None


def test_fail_closed_message_covers_all_reasons() -> None:
    for reason in FailClosedReason:
        message = fail_closed_message(reason)
        assert isinstance(message, str)
        assert message
