"""Receipts for context inclusion, not claims about a model's hidden reasoning."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jarvis.models.jarvis_types import JarvisMemoryEntry


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def memory_reference(memory: JarvisMemoryEntry) -> dict[str, Any]:
    # Deliberately do not expose arbitrary metadata (which can contain secrets).
    artifact = memory.metadata.get("amul_artifact")
    if isinstance(artifact, dict):
        artifact = {
            k: v
            for k, v in artifact.items()
            if k in {"artifact_id", "content_sha256"} and isinstance(v, str) and len(v) <= 512
        } or None
    elif not isinstance(artifact, str) or len(artifact) > 512:
        artifact = None
    ledger_id = memory.metadata.get("continuity_ledger_id")
    lifecycle = memory.metadata.get("status")

    def _id(value: Any) -> str | None:
        return value if isinstance(value, str) and 0 < len(value) <= 512 else None

    return {
        "memory_id": memory.memory_id,
        "session_id": memory.session_id,
        "content_sha256": content_hash(memory.content),
        "status": lifecycle if lifecycle in ("draft", "active", "archived", "superseded") else "legacy_unreviewed",
        "ledger_memory_id": _id(ledger_id),
        "reconciled": bool(memory.metadata.get("reconciled")) if ledger_id else False,
        "supersedes": _id(memory.metadata.get("supersedes")),
        "superseded_by": _id(memory.metadata.get("superseded_by")),
        "amul_artifact": artifact,
        "artifact_verification": "not_checked" if artifact else "not_linked",
    }


def citation(
    content: str,
    excerpt: str,
    *,
    session_id: str,
    source_type: str,
    memory: JarvisMemoryEntry | None = None,
    checkpoint_id: str | None = None,
    role: str | None = None,
    message_ref: str | None = None,
) -> dict[str, Any]:
    item = {
        **(memory_reference(memory) if memory else {}),
        "source_type": source_type,
        "memory_id": memory.memory_id if memory else None,
        "session_id": session_id,
        "content_sha256": content_hash(content),
        "excerpt_sha256": content_hash(excerpt),
        "excerpt_chars": len(excerpt),
        "truncated": excerpt != content,
        "checkpoint_id": checkpoint_id,
        "role": role,
        "message_ref": message_ref,
    }
    item["citation_id"] = "ctx-" + content_hash(json.dumps(item, sort_keys=True, separators=(",", ":")))
    return item


def context_receipt(citations: list[dict[str, Any]], result: Any) -> dict[str, Any]:
    accepted = result is not None and not result.safe_mode and result.inference_status == "accepted"
    return {
        "version": 1,
        "status": "included" if accepted else "no_inference" if result else "not_requested",
        "meaning": "Included in the accepted model request; not proof of causal influence or factual truth.",
        "citations": citations if accepted else [],
    }
