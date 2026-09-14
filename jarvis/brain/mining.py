"""Mine recent governance-check outcomes into per-rule and combination stats.

Report-only: does not write rules, change lane precedence, or apply weights.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from jarvis.governance.schemas import GovernanceCheckOutcome

OUTCOME_VALUES = tuple(item.value for item in GovernanceCheckOutcome)


def mine_governance_outcomes(
    rows: list[dict[str, Any]],
    *,
    min_memory_count: int = 5,
) -> dict[str, Any]:
    """Summarize the last N recorded checks.

    ``success_rate`` is (success + passed) / n for that rule. Combinations are
    co-occurring rule ids on the same turn, labeled by whether the turn had a
    violation.
    """

    if len(rows) < min_memory_count:
        return {
            "status": "insufficient_data",
            "checks_mined": len(rows),
            "min_memory_count": min_memory_count,
            "rules": {},
            "combinations": [],
        }

    by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    by_turn: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        rule_id = str(row.get("rule_id") or "")
        outcome = str(row.get("outcome") or "")
        turn_id = str(row.get("turn_id") or "")
        if not rule_id or outcome not in OUTCOME_VALUES:
            continue
        by_rule[rule_id][outcome] += 1
        if turn_id:
            by_turn[turn_id].append({"rule_id": rule_id, "outcome": outcome})

    rules: dict[str, Any] = {}
    for rule_id, counts in sorted(by_rule.items()):
        n = sum(counts.values())
        success = counts[GovernanceCheckOutcome.SUCCESS.value]
        passed = counts[GovernanceCheckOutcome.PASSED.value]
        violation = counts[GovernanceCheckOutcome.VIOLATION.value]
        unnecessary = counts[GovernanceCheckOutcome.UNNECESSARY.value]
        success_rate = round((success + passed) / n, 4) if n else 0.0
        violation_rate = round(violation / n, 4) if n else 0.0
        rules[rule_id] = {
            "n": n,
            "success": success,
            "passed": passed,
            "violation": violation,
            "unnecessary": unnecessary,
            "success_rate": success_rate,
            "violation_rate": violation_rate,
            "suggested_weight": success_rate,
        }

    combinations = _combination_stats(by_turn)
    return {
        "status": "ok",
        "checks_mined": len(rows),
        "min_memory_count": min_memory_count,
        "rules": rules,
        "combinations": combinations,
    }


def _combination_stats(by_turn: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    combo_counts: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    for checks in by_turn.values():
        rule_ids = tuple(sorted({item["rule_id"] for item in checks}))
        if not rule_ids:
            continue
        blocked = any(item["outcome"] == GovernanceCheckOutcome.VIOLATION.value for item in checks)
        combo_counts[rule_ids]["blocked" if blocked else "clean"] += 1
    ranked: list[dict[str, Any]] = []
    for rule_ids, counts in combo_counts.items():
        n = counts["blocked"] + counts["clean"]
        ranked.append(
            {
                "rules": list(rule_ids),
                "n": n,
                "blocked": counts["blocked"],
                "clean": counts["clean"],
            }
        )
    ranked.sort(key=lambda item: (-item["n"], item["rules"]))
    return ranked[:20]
