from __future__ import annotations

import pytest
from pydantic import BaseModel

from jarvis.governance.policy import compose_policy_lanes
from jarvis.governance.schemas import (
    AuditLedgerEventType,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
    TurnDecision,
    build_audit_ledger_event,
)
from jarvis.governance.secrets import omit_secrets


def test_audit_hash_binds_immutable_event_metadata() -> None:
    event = build_audit_ledger_event(
        event_id="e1",
        turn_id="t1",
        session_id="s1",
        event_type=AuditLedgerEventType.TURN_RECORDED,
        payload={"ok": True},
        previous_event_hash=None,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert event.verify_integrity()
    event.event_type = AuditLedgerEventType.TOMBSTONE
    assert not event.verify_integrity()


class Credentials(BaseModel):
    api_key: str


def test_structured_models_are_sanitized() -> None:
    assert omit_secrets({"request": Credentials(api_key="secret")}) == {"request": {}}


def test_read_only_policy_must_block() -> None:
    with pytest.raises(ValueError):
        PolicyDecision(lane=PolicyLaneName.SAFETY, effect=PolicyEffect.READ_ONLY, blocking=False)


def test_allow_preserves_proposed_decision() -> None:
    result = compose_policy_lanes([], PolicyContext(session_id="s", turn_id="t", proposed_decision=TurnDecision.PLAN))
    assert result.suggested_turn_decision is TurnDecision.PLAN
