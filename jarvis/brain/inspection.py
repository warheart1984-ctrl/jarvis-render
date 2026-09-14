"""Read-only, owner-scoped inspection of memory records and audited context receipts."""

from __future__ import annotations

import json
from typing import Any

from jarvis.brain.provenance import content_hash, memory_reference
from jarvis.core.config import settings
from jarvis.models.jarvis_types import JarvisState


def inspect_session(engine: Any, state: JarvisState, *, recall_key: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "session_id": state.session_id,
        "status": "available",
        "read_only": True,
        "governed_writes_enabled": settings.governed_writes_allowed(),
        "new_memory_status": "draft",
        "records": [],
        "turns": [],
        "withheld_records": 0,
        "previous_session": {"status": "not_used"},
        "evolution": {"status": "none", "auto_applied": False, "applied_changes": []},
    }
    latest = engine.store.latest_evolution_report()
    if latest:
        result["evolution"] = {
            "status": latest.get("status", "ok"),
            "report_id": latest.get("report_id"),
            "created_at": latest.get("created_at"),
            "auto_applied": False,
            "applied_changes": latest.get("applied_changes") or [],
            "checks_mined": latest.get("checks_mined"),
            "suggestion_count": len(latest.get("suggestions") or []),
        }
    if not engine.audit.verify(state.session_id):
        result["status"] = "unverified"
        return result
    if engine.recall.is_revoked(state.session_id):
        result["status"] = "withheld"
        return result
    events = engine.audit.list(state.session_id)
    payloads = {e["turn_id"]: json.loads(e["payload_json"]) for e in events if e["event_type"] == "spiral_turn"}
    # Source all receipts from verified audit payloads, not mutable trace evidence_json.
    for turn in engine.store.load_session_turns(state.session_id, limit=20):
        payload = payloads.get(turn["turn_id"], {})
        if payload.get("content_sha256") != content_hash(turn["content"]):
            result.update(status="unverified", turns=[])
            return result
        result["turns"].append(
            {
                "turn_id": turn["turn_id"],
                "transaction_id": payload.get("transaction_id"),
                "correlation_id": payload.get("correlation_id"),
                "context_receipt": payload.get("runtime_context", {}).get(
                    "context_receipt", {"status": "not_recorded", "citations": []}
                ),
                "deliberation": payload.get("deliberation")
                or {"status": "not_recorded", "stages": [], "committed": False},
                "claims": payload.get("claims") or [],
                "unsupported_claim_warning": payload.get("unsupported_claim_warning"),
            }
        )

    def add_records(source: JarvisState, checkpoint: str | None = None, wanted: set[str] | None = None) -> None:
        stored = {m["id"]: m for m in engine.store.inspect_memories(source.session_id)}
        anchors = {
            p["memory_record"]["memory_id"]: p["memory_record"]
            for e in engine.audit.list(source.session_id)
            if e["event_type"] == "spiral_turn"
            if (p := json.loads(e["payload_json"])).get("memory_record")
        }
        for memory in source.long_term_memory:
            if memory.user_id != state.user_id or memory.session_id != source.session_id:
                continue
            if wanted is not None and memory.memory_id not in wanted:
                continue
            ref = memory_reference(memory)
            row = stored.get(memory.memory_id)
            anchor = anchors.get(memory.memory_id)
            if (
                not row
                or row["subject"] != state.user_id
                or row["content"] != memory.content
                or row["content_sha256"] != ref["content_sha256"]
                or (anchor is not None and (anchor != ref or row["status"] != anchor["status"]))
            ):
                result["withheld_records"] += 1
                continue
            result["records"].append(
                {
                    **ref,
                    "status": row["status"] if anchor else "legacy_unreviewed",
                    "storage_status": row["status"],
                    "integrity": "audit_bound" if anchor else "hash_matches_storage",
                    "checkpoint_id": checkpoint,
                    "preview": memory.content[:1000],
                    "preview_truncated": len(memory.content) > 1000,
                    "created_at": memory.created_at,
                }
            )

    add_records(state)
    # Do not fetch a different/opted-out conversation simply to populate the panel.
    last = next(reversed(payloads.values()), {})
    used = last.get("runtime_context", {}).get("previous_session", {})
    if used.get("status") == "verified" and (recall_key or state.user_id == settings.recall_owner_user_id):
        previous = engine.recall.previous(
            state.user_id, recall_key or settings.service_token, session_id=state.session_id, before=state.created_at
        )
        result["previous_session"] = previous.metadata
        if (
            previous.state
            and previous.metadata.get("checkpoint_id") == used.get("checkpoint_id")
            and previous.metadata.get("source_session_id") == used.get("source_session_id")
        ):
            wanted = {
                c["memory_id"]
                for t in result["turns"]
                for c in t["context_receipt"].get("citations", [])
                if c.get("checkpoint_id") == used["checkpoint_id"] and c.get("memory_id")
            }
            add_records(previous.state, used["checkpoint_id"], wanted)
        elif previous.state:
            result["previous_session"] = {"status": "unverified"}
    return result
