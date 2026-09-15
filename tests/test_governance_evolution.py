"""Governance-outcome learning loop: record, mine, report — never auto-apply rules."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from jarvis.brain.engine import JarvisEngine
from jarvis.brain.evolution import EvolutionEngine, run_evolution
from jarvis.brain.inspection import inspect_session
from jarvis.brain.llm import LLMResult
from jarvis.brain.mining import mine_governance_outcomes
from jarvis.core.config import settings
from jarvis.governance.lanes import SafetyPolicyLane, evaluate_policies
from jarvis.governance.outcomes import classify_lane_outcome, outcomes_from_policy
from jarvis.governance.policy import LANE_PRECEDENCE
from jarvis.governance.schemas import (
    CompositePolicyDecision,
    GovernanceCheckOutcome,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
    TurnDecision,
)
from jarvis.models.jarvis_types import ChatRequest
from jarvis.persistence import JarvisStore

ALICE = ("t_alice", "google-sub-alice")
BOB = ("t_bob", "google-sub-bob")


def _row(*, rule_id: str, outcome: str, session_id: str = "s1", turn_id: str | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "outcome_id": uuid4().hex,
        "session_id": session_id,
        "turn_id": turn_id or uuid4().hex,
        "rule_id": rule_id,
        "outcome": outcome,
        "feedback": None,
        "created_at": now,
        "event_hash": "abc",
    }


def test_classify_lane_outcomes_cover_the_contract() -> None:
    allow = PolicyDecision(lane=PolicyLaneName.SAFETY, effect=PolicyEffect.ALLOW, blocking=False)
    block = PolicyDecision(
        lane=PolicyLaneName.SAFETY,
        effect=PolicyEffect.FAIL_CLOSED,
        blocking=True,
        fail_closed_reason=None,
    )
    assert classify_lane_outcome(allow, turn_decision="answer") == GovernanceCheckOutcome.SUCCESS.value
    assert classify_lane_outcome(allow, turn_decision="fail_closed") == GovernanceCheckOutcome.PASSED.value
    assert classify_lane_outcome(block, turn_decision="fail_closed") == GovernanceCheckOutcome.VIOLATION.value


def test_outcomes_from_policy_are_one_row_per_lane() -> None:
    policy = evaluate_policies(PolicyContext(session_id="s", turn_id="t", uncertainty=0.1, stress=0.0))
    rows = outcomes_from_policy(policy, session_id="s", turn_id="t", turn_decision="answer", event_hash="h")
    assert len(rows) == 6
    assert {row["outcome"] for row in rows} == {GovernanceCheckOutcome.SUCCESS.value}
    assert all(row["event_hash"] == "h" for row in rows)


def test_mining_needs_a_minimum_check_count() -> None:
    mined = mine_governance_outcomes(
        [{"rule_id": "safety", "outcome": "success", "turn_id": "t1"}],
        min_memory_count=5,
    )
    assert mined["status"] == "insufficient_data"
    assert mined["rules"] == {}


def test_mining_success_rates_and_combinations() -> None:
    rows = []
    for _ in range(3):
        turn = uuid4().hex
        rows.append({"rule_id": "safety", "outcome": "success", "turn_id": turn})
        rows.append({"rule_id": "audit", "outcome": "success", "turn_id": turn})
    blocked = uuid4().hex
    rows.append({"rule_id": "safety:uncertainty", "outcome": "violation", "turn_id": blocked})
    rows.append({"rule_id": "audit", "outcome": "passed", "turn_id": blocked})
    mined = mine_governance_outcomes(rows, min_memory_count=5)
    assert mined["status"] == "ok"
    assert mined["rules"]["safety"]["success_rate"] == 1.0
    assert mined["rules"]["safety:uncertainty"]["violation_rate"] == 1.0
    assert mined["rules"]["audit"]["success"] == 3
    combos = {tuple(item["rules"]): item for item in mined["combinations"]}
    assert combos[("audit", "safety")]["clean"] == 3
    assert combos[("audit", "safety:uncertainty")]["blocked"] == 1


@pytest.mark.asyncio
async def test_engine_records_outcomes_and_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("ok", "test", "m", 1)),
    )
    engine = JarvisEngine(store=JarvisStore(tmp_path / "on.sqlite3"))
    state = await engine.get_or_create_session("owner")
    state.confidence = 0.9
    await engine.chat(ChatRequest(user_id="owner", session_id=state.session_id, message="Hello there friend"))
    recorded = engine.store.load_governance_outcomes()
    assert recorded
    assert {row["outcome"] for row in recorded} <= set(item.value for item in GovernanceCheckOutcome)
    assert all(row["event_hash"] for row in recorded)

    monkeypatch.setattr(settings, "governance_outcomes_enabled", False)
    quiet = JarvisEngine(store=JarvisStore(tmp_path / "off.sqlite3"))
    quiet_state = await quiet.get_or_create_session("owner")
    quiet_state.confidence = 0.9
    await quiet.chat(ChatRequest(user_id="owner", session_id=quiet_state.session_id, message="Hello there friend"))
    assert quiet.store.load_governance_outcomes() == []


@pytest.mark.asyncio
async def test_tenant_isolation_of_outcomes_and_reports(tmp_path):
    path = tmp_path / "tenants.sqlite3"
    alice = JarvisStore(path, *ALICE)
    bob = JarvisStore(path, *BOB)
    turn = uuid4().hex
    for i in range(5):
        alice.save_governance_outcomes(
            [_row(rule_id="safety", outcome="success", turn_id=f"{turn}-{i}")]
        )
    alice_report = run_evolution(alice, min_memory_count=5)
    assert alice.load_governance_outcomes()
    assert bob.load_governance_outcomes() == []
    assert alice.latest_evolution_report()["report_id"] == alice_report["report_id"]
    assert bob.latest_evolution_report() is None


def test_evolution_cycle_is_report_only_and_never_auto_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "evolution_enabled", True)
    store = JarvisStore(tmp_path / "evo.sqlite3")
    turn = uuid4().hex
    rows = []
    for i in range(8):
        rows.append(_row(rule_id="safety", outcome="success", turn_id=f"{turn}-ok-{i}"))
        rows.append(_row(rule_id="audit", outcome="success", turn_id=f"{turn}-ok-{i}"))
    store.save_governance_outcomes(rows)
    before = tuple(LANE_PRECEDENCE)
    report = EvolutionEngine(store).run_cycle(min_memory_count=5)
    after = tuple(LANE_PRECEDENCE)
    assert before == after
    assert report["auto_applied"] is False
    assert report["applied_changes"] == []
    assert report["policy_authoritative"] is True
    assert store.latest_evolution_report()["auto_applied"] is False
    # Hard-coded safety lane still fail-closes; suggested weights did not rewrite it.
    blocked = SafetyPolicyLane().evaluate(PolicyContext(session_id="s", turn_id="t", stress=0.95))
    assert blocked.blocking is True
    allow = evaluate_policies(PolicyContext(session_id="s", turn_id="t", uncertainty=0.1, stress=0.0))
    assert allow.effect is PolicyEffect.ALLOW
    assert isinstance(allow, CompositePolicyDecision)
    assert allow.proposed_decision is TurnDecision.ANSWER


@pytest.mark.asyncio
async def test_inspection_surfaces_report_without_claiming_apply(tmp_path):
    store = JarvisStore(tmp_path / "inspect.sqlite3")
    engine = JarvisEngine(store=store)
    for i in range(5):
        store.save_governance_outcomes(
            [_row(rule_id="safety", outcome="success", turn_id=f"t{i}")]
        )
    report = run_evolution(store, min_memory_count=5)
    state = await engine.get_or_create_session("owner")
    data = inspect_session(engine, state)
    assert data["evolution"]["report_id"] == report["report_id"]
    assert data["evolution"]["auto_applied"] is False
    assert data["evolution"]["applied_changes"] == []
    assert data["status"] == "unverified"
