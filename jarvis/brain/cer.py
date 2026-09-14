"""Constitutional Execution Record assembled onto the existing spiral_turn audit.

No fifth ledger. Replay uses hashes and identifiers already bound to the turn.
"""

from __future__ import annotations

from typing import Any

from jarvis.brain.provenance import content_hash
from jarvis.governance.secrets import omit_secrets

CER_VERSION = "jarvis-cer-v1"


def _challenge_status(deliberation: dict[str, Any]) -> str:
    for stage in deliberation.get("stages") or []:
        if stage.get("name") == "challenge":
            return str(stage.get("status") or "not_recorded")
    return "not_recorded"


def _observe_tools(observe: dict[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for record in observe.get("records") or []:
        citations = [
            {
                "locator": item.get("locator"),
                "content_sha256": item.get("content_sha256"),
            }
            for item in (record.get("citations") or [])
            if isinstance(item, dict)
        ]
        tools.append(
            {
                "tool": record.get("tool"),
                "status": record.get("status"),
                "observed": bool(record.get("observed")),
                "args_hash": record.get("args_hash") or "",
                "payload_hash": record.get("payload_hash") or "",
                "citations": citations,
            }
        )
    return tools


def build_cer_record(
    *,
    session_id: str,
    turn_id: str,
    transaction_id: str,
    correlation_id: str,
    user_message: str,
    reply: str,
    provider: str,
    model: str,
    inference_status: str,
    fallback_used: bool,
    safe_mode: bool,
    deliberation: dict[str, Any],
    claims: list[dict[str, Any]],
    observe: dict[str, Any] | None,
    context_receipt: dict[str, Any] | None,
    previous_turn_id: str | None,
) -> dict[str, Any]:
    observe = observe or {}
    receipt = context_receipt or {}
    input_sha = content_hash(user_message)
    output_sha = content_hash(reply)
    record = {
        "version": CER_VERSION,
        "schema": "constitutional_execution_record",
        "model_identity": {
            "provider": provider,
            "model": model,
            "inference_status": inference_status,
            "fallback_used": bool(fallback_used),
            "safe_mode": bool(safe_mode),
        },
        "plan": {
            "stages": list(deliberation.get("stages") or []),
            "committed": bool(deliberation.get("committed")),
        },
        "evidence": {
            "claims": list(claims or []),
            "context_receipt_status": receipt.get("status") or "not_recorded",
            "observe_required": bool(observe.get("required")),
            "observe_thin": bool(observe.get("thin")),
            "observe_tools": _observe_tools(observe),
        },
        "verification": {
            "challenge": _challenge_status(deliberation),
            "committed": bool(deliberation.get("committed")),
            "unsupported_claim_warning": deliberation.get("unsupported_claim_warning"),
            "input_sha256": input_sha,
            "content_sha256": output_sha,
        },
        "replay": {
            "transaction_id": transaction_id,
            "correlation_id": correlation_id,
            "input_sha256": input_sha,
            "content_sha256": output_sha,
        },
        "lineage": {
            "session_id": session_id,
            "turn_id": turn_id,
            "previous_turn_id": previous_turn_id,
        },
    }
    return omit_secrets(record)
