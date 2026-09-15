"""Report-only evolution cycle over mined governance outcomes.

Hard-coded policy lanes remain authoritative. This module never deprecates
rules, never rewrites lane code, and never auto-applies weights.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from jarvis.brain.mining import mine_governance_outcomes
from jarvis.core.config import settings
from jarvis.governance.policy import LANE_PRECEDENCE
from jarvis.governance.schemas import GovernanceCheckOutcome
from jarvis.persistence.store import JarvisStore

logger = logging.getLogger(__name__)


class EvolutionEngine:
    """Mine check outcomes and store a human-readable evolution report."""

    def __init__(self, store: JarvisStore) -> None:
        self.store = store

    def run_cycle(
        self,
        *,
        min_memory_count: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Produce and persist a report. ``auto_applied`` is always false."""

        threshold = min_memory_count if min_memory_count is not None else settings.evolution_min_checks
        mine_limit = limit if limit is not None else settings.evolution_mine_limit
        rows = self.store.load_governance_outcomes(limit=mine_limit)
        mined = mine_governance_outcomes(rows, min_memory_count=threshold)
        report = {
            "report_id": uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": mined["status"],
            "auto_applied": False,
            "policy_authoritative": True,
            "applied_changes": [],
            "authoritative_lanes": [lane.value for lane in LANE_PRECEDENCE],
            "checks_mined": mined["checks_mined"],
            "min_memory_count": mined["min_memory_count"],
            "rules": mined["rules"],
            "combinations": mined["combinations"],
            "suggestions": _suggestions(mined["rules"]),
        }
        self.store.save_evolution_report(report)
        logger.info(
            "Governance evolution report %s status=%s auto_applied=false checks=%s",
            report["report_id"],
            report["status"],
            report["checks_mined"],
        )
        return report


def run_evolution(
    store: JarvisStore,
    *,
    min_memory_count: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for :meth:`EvolutionEngine.run_cycle`."""

    return EvolutionEngine(store).run_cycle(min_memory_count=min_memory_count, limit=limit)


def _suggestions(rules: dict[str, Any]) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    for rule_id, stats in rules.items():
        n = int(stats["n"])
        violation_rate = float(stats["violation_rate"])
        unnecessary = int(stats["unnecessary"])
        action = "keep"
        reason = "Hard-coded lane remains authoritative; weight is a suggestion only."
        if unnecessary > 0 and unnecessary >= max(1, n // 3):
            action = "review_as_possibly_unnecessary"
            reason = "Human labels marked this check unnecessary on several turns; do not auto-drop it."
        elif violation_rate >= 0.5 and n >= 5:
            action = "review_threshold"
            reason = "High violation rate; a human should review the rule, not an automatic deprecation."
        elif float(stats["success_rate"]) >= 0.95 and stats[GovernanceCheckOutcome.VIOLATION.value] == 0 and n >= 8:
            action = "review_as_possibly_unnecessary"
            reason = "Almost never fires; a human may decide it is redundant. Not auto-removed."
        notes.append(
            {
                "rule_id": rule_id,
                "action": action,
                "reason": reason,
                "suggested_weight": str(stats["suggested_weight"]),
            }
        )
    notes.sort(key=lambda item: item["rule_id"])
    return notes
