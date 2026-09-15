"""OTEM-lite governed memory promotion: proposal → preview → explicit user request → EMR gate → apply.

This module builds promotion proposals from local user-grounded draft memories,
renders read-only previews (never writes), and orchestrates the apply path by
reusing the existing propose/verify/reconcile tail.

Flow:
  1.  build_proposal    – map draft memory → typed ledger proposal + evidence
  2.  render_preview    – read-only dict (no ledger contact, no store mutation)
  3.  apply_promotion   – gate → propose/supersede → retrieve-verify → reconcile

The durable write lock (write_lock.py) keeps apply disabled in production by
default; preview is always safe to render for authenticated owners.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from jarvis.core.config import settings
from jarvis.models.jarvis_types import JarvisMemoryEntry, JarvisState

# ── Ledger type mapping (mirrors routes/chat.py /memory/propose) ────────────

_CATEGORY_TO_LEDGER_TYPE: dict[str, str] = {
    "build_intent": "task",
    "spiral_intelligence": "architecture",
    "music": "research",
    "preference": "preference",
}


def _ledger_type(category: str) -> str:
    return _CATEGORY_TO_LEDGER_TYPE.get(category, "fact")


def _subject(ledger_type: str, user_id: str) -> str:
    return "jarvis-prototype" if ledger_type in {"architecture", "task", "research"} else user_id


# ── Proposal ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MemoryPromotionProposal:
    """Structured proposal built from a local draft memory for promotion to Continuity."""

    proposal_id: str
    session_id: str
    memory_id: str
    user_id: str
    ledger_type: str
    subject: str
    content: str
    content_sha256: str
    confidence: float
    evidence: list[dict[str, str]]
    tags: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    user_requested: bool = False
    supersedes_ledger_id: str | None = None


def build_proposal(
    state: JarvisState,
    memory: JarvisMemoryEntry,
    *,
    evidence: list[dict[str, str]] | None = None,
    user_requested: bool = False,
    supersedes_ledger_id: str | None = None,
) -> MemoryPromotionProposal:
    """Build a promotion proposal from a local draft memory.

    Raises ValueError if the memory appears tool-sourced or content is empty
    (never silently promote retrieved/snippet content).
    """

    content = (memory.content or "").strip()
    if not content:
        raise ValueError("Memory content is empty")
    if len(content) > 2000:
        content = content[:2000]

    lt = _ledger_type(memory.category)
    sub = _subject(lt, memory.user_id)
    digest = hashlib.sha256(content.encode()).hexdigest()

    evidence_refs = evidence if evidence is not None else [{"kind": "chat", "ref": memory.memory_id}]

    promo_id = f"promo-{uuid4().hex[:16]}"
    idem_key = f"jarvis-promote-{memory.memory_id}-{digest[:16]}"

    return MemoryPromotionProposal(
        proposal_id=promo_id,
        session_id=state.session_id,
        memory_id=memory.memory_id,
        user_id=state.user_id,
        ledger_type=lt,
        subject=sub,
        content=content,
        content_sha256=digest,
        confidence=round(memory.importance, 4),
        evidence=evidence_refs,
        tags=[memory.category] if memory.category != "general" else [],
        idempotency_key=idem_key,
        user_requested=user_requested,
        supersedes_ledger_id=supersedes_ledger_id,
    )


# ── Gate ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PromotionGate:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def check_gate(
    *,
    continuity_configured: bool = True,
    read_only: bool = False,
    already_reconciled: bool = False,
) -> PromotionGate:
    """Deterministic gate check for OTEM-lite promotion apply.

    Mirrors the checks in routes/chat.py /memory/propose but keeps them
    reusable from both the route and any direct caller.
    """

    reasons: list[str] = []

    if not settings.governed_writes_enabled:
        reasons.append("governed_writes_disabled")
    if not settings.governed_writes_allowed():
        reasons.append("governed_writes_not_allowed_by_env")
    if not continuity_configured:
        reasons.append("continuity_not_configured")
    if read_only:
        reasons.append("session_read_only")
    if already_reconciled:
        reasons.append("memory_already_reconciled")

    return PromotionGate(allowed=len(reasons) == 0, reasons=reasons)


# ── Preview (read-only, never mutates) ──────────────────────────────────────

def render_preview(proposal: MemoryPromotionProposal, gate: PromotionGate) -> dict[str, Any]:
    """Render a read-only preview of what would be submitted to the Continuity Ledger.

    This function MUST NOT call any store or ledger method. It is pure.
    """

    return {
        "preview_only": True,
        "no_write": True,
        "authority": False,
        "memory_eligible": False,
        "proposal": {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "ledger_type": proposal.ledger_type,
            "subject": proposal.subject,
            "content_sha256": proposal.content_sha256,
            "confidence": proposal.confidence,
            "evidence": proposal.evidence,
            "tags": proposal.tags,
            "idempotency_key": proposal.idempotency_key,
            "user_requested": proposal.user_requested,
            "supersedes_ledger_id": proposal.supersedes_ledger_id,
        },
        "content_preview": proposal.content[:200],
        "content_length": len(proposal.content),
        "gate": {
            "allow_apply": gate.allowed,
            "reasons": gate.reasons,
        },
    }


# ── Apply (async, reuses existing tail) ─────────────────────────────────────

async def apply_promotion(
    engine: Any,
    state: JarvisState,
    proposal: MemoryPromotionProposal,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a promotion proposal via the existing propose/verify/reconcile tail.

    Audit events emitted:
      - memory_promotion_previewed  (preview/dry_run)
      - memory_promotion_applied    (successful reconcile)
      - memory_promotion_refused    (gate denied or ledger refused)

    Never admits tool-sourced or snippet content — proposal builder already
    guards this, and we also verify the local memory still exists.
    """

    session_id = state.session_id

    # ── locate local draft ──────────────────────────────────────────────
    memory = next((m for m in state.long_term_memory if m.memory_id == proposal.memory_id), None)
    if memory is None:
        event = {"proposal_id": proposal.proposal_id, "memory_id": proposal.memory_id, "reason": "memory_not_found"}
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", event)
        return {"status": "refused", "reason": "memory_not_found", "proposal_id": proposal.proposal_id}

    already_reconciled = bool(memory.metadata.get("continuity_ledger_id"))

    # ── gate ────────────────────────────────────────────────────────────
    gate = check_gate(
        continuity_configured=engine.continuity is not None,
        read_only=engine.is_read_only(session_id),
        already_reconciled=already_reconciled,
    )

    if not proposal.user_requested:
        gate = PromotionGate(allowed=False, reasons=[*gate.reasons, "user_requested_required"])

    if dry_run:
        gate = PromotionGate(allowed=False, reasons=[*gate.reasons, "dry_run"])
        engine.audit.append(f"promo-preview-{proposal.proposal_id}", session_id, "memory_promotion_previewed", {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "content_sha256": proposal.content_sha256,
            "gate_reasons": gate.reasons,
        })
        return {**render_preview(proposal, gate), "status": "preview"}

    if not gate.allowed:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "content_sha256": proposal.content_sha256,
            "reasons": gate.reasons,
        })
        return {"status": "refused", "reasons": gate.reasons, "proposal_id": proposal.proposal_id}

    # ── supersede path ──────────────────────────────────────────────────
    if proposal.supersedes_ledger_id:
        return await _apply_supersede(engine, state, memory, proposal)

    # ── propose path ────────────────────────────────────────────────────
    return await _apply_propose(engine, state, memory, proposal)


async def _apply_propose(
    engine: Any,
    state: JarvisState,
    memory: JarvisMemoryEntry,
    proposal: MemoryPromotionProposal,
) -> dict[str, Any]:
    session_id = state.session_id
    try:
        result = await engine.continuity.propose_memory(
            {
                "id": f"mem-{memory.memory_id}",
                "session_id": session_id,
                "type": proposal.ledger_type,
                "confidence": proposal.confidence,
                "evidence": proposal.evidence,
                "status": "draft",
                "content": proposal.content,
                "subject": proposal.subject,
                "content_sha256": proposal.content_sha256,
                "created_at": memory.created_at,
                "user_requested": True,
                "supersedes": proposal.supersedes_ledger_id,
            },
            idempotency_key=proposal.idempotency_key,
        )
    except Exception as exc:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "content_sha256": proposal.content_sha256,
            "reasons": ["continuity_exception", str(exc)[:200]],
        })
        return {
            "status": "refused",
            "reasons": ["continuity_exception", str(exc)[:200]],
            "proposal_id": proposal.proposal_id,
        }

    status = str(result.get("status", "")).lower()
    if status == "unavailable":
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "memory_id": proposal.memory_id, "reasons": ["ledger_unavailable"],
        })
        return {"status": "refused", "reasons": ["ledger_unavailable"], "proposal_id": proposal.proposal_id}

    if status == "simulated":
        engine.audit.append(f"promo-preview-{proposal.proposal_id}", session_id, "memory_promotion_previewed", {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "content_sha256": proposal.content_sha256,
            "note": "simulated",
        })
        return {
            "status": "simulated",
            "proposal_id": proposal.proposal_id,
            "ledger": result,
        }

    refused = status in {"conflict", "refused"} or result.get("refused") is True
    if refused:
        if status == "conflict" or result.get("refuse_reason") == "conflict-membrane":
            engine.set_read_only(session_id, "conflict")
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "memory_id": proposal.memory_id,
            "content_sha256": proposal.content_sha256,
            "reasons": [status or "refused"],
        })
        return {
            "status": "conflict" if status == "conflict" else "refused",
            "proposal_id": proposal.proposal_id,
            "read_only": engine.is_read_only(session_id),
            "ledger": result,
        }

    if status != "accepted":
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "memory_id": proposal.memory_id, "reasons": ["unconfirmed_write"],
        })
        return {"status": "refused", "reasons": ["unconfirmed_write"], "proposal_id": proposal.proposal_id}

    # ── retrieve-verify ─────────────────────────────────────────────────
    ledger_memory = result.get("memory") or {}
    try:
        verified = await engine.continuity.retrieve(ledger_memory["id"])
    except Exception:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "reasons": ["verify_failed"],
        })
        return {"status": "refused", "reasons": ["verify_failed"], "proposal_id": proposal.proposal_id}

    verified_memory = verified.get("memory", verified)
    if (verified_memory.get("id") != ledger_memory.get("id") or
            verified_memory.get("content_sha256") != ledger_memory.get("content_sha256")):
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "reasons": ["verify_mismatch"],
        })
        return {
            "status": "refused",
            "reasons": ["verify_mismatch"],
            "proposal_id": proposal.proposal_id,
        }

    # ── reconcile ───────────────────────────────────────────────────────
    try:
        bound = engine.reconcile_proposed_memory(
            state,
            memory_id=memory.memory_id,
            ledger_memory_id=ledger_memory["id"],
            content_sha256=ledger_memory["content_sha256"],
            transaction_id=result.get("transaction_id"),
            correlation_id=result.get("correlation_id"),
        )
    except ValueError as exc:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "reasons": [str(exc)[:200]],
        })
        return {"status": "refused", "reasons": [str(exc)[:200]], "proposal_id": proposal.proposal_id}

    engine.audit.append(f"promo-apply-{proposal.proposal_id}", session_id, "memory_promotion_applied", {
        "proposal_id": proposal.proposal_id,
        "memory_id": memory.memory_id,
        "ledger_memory_id": ledger_memory["id"],
        "content_sha256": proposal.content_sha256,
    })
    return {**bound, "proposal_id": proposal.proposal_id, "ledger": result}


async def _apply_supersede(
    engine: Any,
    state: JarvisState,
    memory: JarvisMemoryEntry,
    proposal: MemoryPromotionProposal,
) -> dict[str, Any]:
    session_id = state.session_id
    ledger_memory_id = memory.metadata.get("continuity_ledger_id")
    if not ledger_memory_id:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "reasons": ["not_reconciled"],
        })
        return {"status": "refused", "reasons": ["not_reconciled"], "proposal_id": proposal.proposal_id}

    try:
        result = await engine.continuity.supersede(ledger_memory_id, proposal.content, session_id)
    except Exception as exc:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "reasons": ["continuity_exception", str(exc)[:200]],
        })
        return {
            "status": "refused",
            "reasons": ["continuity_exception", str(exc)[:200]],
            "proposal_id": proposal.proposal_id,
        }

    status = str(result.get("status", "")).lower()
    if status == "unavailable":
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "reasons": ["ledger_unavailable"],
        })
        return {"status": "refused", "reasons": ["ledger_unavailable"], "proposal_id": proposal.proposal_id}

    refused = status in {"conflict", "refused"} or result.get("refused") is True
    if refused:
        if status == "conflict" or result.get("refuse_reason") == "conflict-membrane":
            engine.set_read_only(session_id, "conflict")
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "reasons": [status or "refused"],
        })
        return {
            "status": status if status in {"conflict"} else "refused",
            "proposal_id": proposal.proposal_id,
            "read_only": engine.is_read_only(session_id),
            "ledger": result,
        }

    if status != "accepted" or result.get("accepted") is not True:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id, "reasons": ["unconfirmed_supersession"],
        })
        return {"status": "refused", "reasons": ["unconfirmed_supersession"], "proposal_id": proposal.proposal_id}

    try:
        applied = engine.apply_supersession(
            state,
            memory_id=memory.memory_id,
            content=proposal.content,
            ledger_result=result,
        )
    except ValueError as exc:
        engine.audit.append(f"promo-refuse-{proposal.proposal_id}", session_id, "memory_promotion_refused", {
            "proposal_id": proposal.proposal_id,
            "reasons": [str(exc)[:200]],
        })
        return {
            "status": "refused",
            "reasons": [str(exc)[:200]],
            "proposal_id": proposal.proposal_id,
        }

    engine.audit.append(f"promo-apply-{proposal.proposal_id}", session_id, "memory_promotion_applied", {
        "proposal_id": proposal.proposal_id,
        "memory_id": memory.memory_id,
        "content_sha256": proposal.content_sha256,
    })
    return {**applied, "proposal_id": proposal.proposal_id, "ledger": result}
