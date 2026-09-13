"""PolicyLane protocol and precedence-preserving composition runner.

Precedence is fixed:

    Safety → Continuity/memory auth → Audit → Cost/provider
    → Drift/checkpoint → SideEffect

The first blocking decision (``fail_closed`` or ``read_only``) wins.
Later lanes are still evaluated and recorded, but they cannot loosen an
earlier block. A blocking Safety result therefore cannot be overridden
by Cost, Drift, or SideEffect.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from jarvis.governance.schemas import (
    CompositePolicyDecision,
    FailClosedReason,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
)

LANE_PRECEDENCE: tuple[PolicyLaneName, ...] = (
    PolicyLaneName.SAFETY,
    PolicyLaneName.CONTINUITY,
    PolicyLaneName.AUDIT,
    PolicyLaneName.COST,
    PolicyLaneName.DRIFT,
    PolicyLaneName.SIDE_EFFECT,
)


@runtime_checkable
class PolicyLane(Protocol):
    """Composable policy lane evaluated against a :class:`PolicyContext`."""

    name: PolicyLaneName

    def evaluate(self, context: PolicyContext) -> PolicyDecision: ...


def _precedence_index(name: PolicyLaneName) -> int:
    try:
        return LANE_PRECEDENCE.index(name)
    except ValueError:
        return len(LANE_PRECEDENCE)


def compose_policy_lanes(
    lanes: Sequence[PolicyLane],
    context: PolicyContext,
) -> CompositePolicyDecision:
    """Evaluate ``lanes`` in precedence order and freeze the first block.

    Non-blocking ``allow`` results never override an earlier block.
    Later blocking results are recorded but ignored for the composite
    effect once a prior lane has already blocked.
    """

    ordered = sorted(lanes, key=lambda lane: _precedence_index(lane.name))
    lane_decisions: list[PolicyDecision] = []
    effect = PolicyEffect.ALLOW
    blocking = False
    fail_closed_reason: FailClosedReason | None = None
    blocking_lane: PolicyLaneName | None = None

    for lane in ordered:
        decision = lane.evaluate(context)
        if decision.lane is not lane.name:
            raise ValueError(f"lane {lane.name.value} returned decision for {decision.lane.value}")
        lane_decisions.append(decision)

        if blocking:
            continue
        if not decision.blocking:
            continue

        blocking = True
        effect = decision.effect
        blocking_lane = decision.lane
        fail_closed_reason = decision.fail_closed_reason

    return CompositePolicyDecision(
        effect=effect,
        blocking=blocking,
        fail_closed_reason=fail_closed_reason,
        blocking_lane=blocking_lane,
        lane_decisions=lane_decisions,
        proposed_decision=context.proposed_decision,
    )
