"""Observe-only mapping from policy lane results to governance check outcomes.

Hard-coded lanes stay authoritative. These records never mutate policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, assert_never
from uuid import uuid4

from jarvis.governance.schemas import (
    CompositePolicyDecision,
    GovernanceCheckOutcome,
    PolicyDecision,
    PolicyEffect,
)

MAX_FEEDBACK_CHARS = 500
VALID_OUTCOMES = frozenset(item.value for item in GovernanceCheckOutcome)


def rule_id_for(decision: PolicyDecision) -> str:
    if decision.fail_closed_reason is not None:
        return f"{decision.lane.value}:{decision.fail_closed_reason.value}"
    return decision.lane.value


def classify_lane_outcome(decision: PolicyDecision, *, turn_decision: str) -> str:
    """Map one lane result plus the turn's actual decision to a check outcome."""

    effect = decision.effect
    if effect is PolicyEffect.FAIL_CLOSED or effect is PolicyEffect.READ_ONLY:
        return GovernanceCheckOutcome.VIOLATION.value
    if effect is PolicyEffect.ALLOW:
        if turn_decision == "answer":
            return GovernanceCheckOutcome.SUCCESS.value
        return GovernanceCheckOutcome.PASSED.value
    assert_never(effect)


def outcomes_from_policy(
    policy: CompositePolicyDecision,
    *,
    session_id: str,
    turn_id: str,
    turn_decision: str,
    event_hash: str | None = None,
    feedback: str | None = None,
) -> list[dict[str, Any]]:
    """Build tenant-ready outcome rows. Caller persists; nothing is auto-applied."""

    note = clip_feedback(feedback)
    created_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for decision in policy.lane_decisions:
        rows.append(
            {
                "outcome_id": uuid4().hex,
                "session_id": session_id,
                "turn_id": turn_id,
                "rule_id": rule_id_for(decision),
                "outcome": classify_lane_outcome(decision, turn_decision=turn_decision),
                "feedback": note,
                "created_at": created_at,
                "event_hash": event_hash,
            }
        )
    return rows


def clip_feedback(feedback: str | None) -> str | None:
    if feedback is None:
        return None
    text = str(feedback).strip()
    if not text:
        return None
    if len(text) <= MAX_FEEDBACK_CHARS:
        return text
    return text[: MAX_FEEDBACK_CHARS - 3].rstrip() + "..."
