"""Chat routes — the main conversational endpoint for Jarvis."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import ProviderError
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])

engine = JarvisEngine()


class MemoryProposal(BaseModel):
    session_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    user_requested: bool = False


class MemorySupersession(BaseModel):
    session_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    user_requested: bool = False


@router.post("/memory/propose")
async def propose_memory(request: MemoryProposal) -> dict[str, Any]:
    if not request.user_requested:
        raise HTTPException(status_code=400, detail="Explicit user_requested=true is required")
    if engine.continuity is None:
        raise HTTPException(status_code=503, detail="Continuity Ledger is not configured")
    state = engine.get_session(request.session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if state.user_id != request.user_id:
        raise HTTPException(status_code=403, detail="Session ownership check failed")
    memory = next((m for m in state.long_term_memory if m.memory_id == request.memory_id), None)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    ledger_type = {
        "build_intent": "task",
        "spiral_intelligence": "architecture",
        "music": "research",
        "preference": "preference",
    }.get(memory.category, "fact")
    subject = "jarvis-prototype" if ledger_type in {"architecture", "task", "research"} else state.user_id
    try:
        result = await engine.continuity.propose_memory(
            {
                "id": f"mem-{memory.memory_id}",
                "session_id": state.session_id,
                "type": ledger_type,
                "confidence": memory.importance,
                "evidence": [{"kind": "chat", "ref": memory.memory_id}],
                "status": "draft",
                "content": memory.content,
                "subject": subject,
                "content_sha256": engine.store.content_hash(memory.content),
                "created_at": memory.created_at,
                "user_requested": True,
            },
            idempotency_key=f"jarvis-memory-{memory.memory_id}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"message": "Continuity Ledger unavailable or refused the proposal", "error": str(exc)},
        ) from exc
    if result.get("status") == "unavailable":
        raise HTTPException(status_code=503, detail="Continuity Ledger unavailable; durable storage not confirmed")
    if result.get("status") == "simulated":
        return {"status": "simulated", "memory_id": memory.memory_id, "durable": False, "ledger": result}
    if result.get("status") in {"conflict", "refused"} or result.get("refused") is True:
        engine.set_read_only(state.session_id)
        engine.audit.append(
            f"conflict-{request.memory_id}",
            state.session_id,
            "conflict_membrane",
            {
                "memory_id": request.memory_id,
                "reason": result.get("refuse_reason"),
                "conflicts": result.get("conflicts", []),
                "mode": "read_only",
            },
        )
        return {"status": "conflict", "memory_id": memory.memory_id, "read_only": True, "ledger": result}
    if result.get("status") != "accepted":
        raise HTTPException(status_code=502, detail="Continuity Ledger returned an unconfirmed write result")
    ledger_memory = result.get("memory") or {}
    try:
        verified = await engine.continuity.retrieve(ledger_memory["id"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Ledger accepted write but retrieval verification failed") from exc
    verified_memory = verified.get("memory", verified)
    if verified_memory.get("id") != ledger_memory.get("id") or verified_memory.get(
        "content_sha256"
    ) != ledger_memory.get("content_sha256"):
        raise HTTPException(status_code=502, detail="Ledger write verification hash or ID mismatch")
    memory.metadata["continuity_ledger_id"] = ledger_memory["id"]
    return {
        "status": "durably_stored",
        "memory_id": memory.memory_id,
        "ledger_memory_id": (result.get("memory") or {}).get("id"),
        "ledger": result,
    }


@router.post("/memory/supersede")
async def supersede_memory(
    request: MemorySupersession, x_jarvis_service_token: str = Header(default="")
) -> dict[str, Any]:
    if not settings.service_token or not secrets.compare_digest(x_jarvis_service_token, settings.service_token):
        raise HTTPException(status_code=401, detail="Supersession requires a valid service token")
    if not request.user_requested:
        raise HTTPException(status_code=400, detail="Explicit user_requested=true is required")
    if engine.continuity is None:
        raise HTTPException(status_code=503, detail="Continuity Ledger is not configured")
    state = engine.get_session(request.session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if state.user_id != request.user_id:
        raise HTTPException(status_code=403, detail="Session ownership check failed")
    memory = next((m for m in state.long_term_memory if m.memory_id == request.memory_id), None)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    ledger_memory_id = memory.metadata.get("continuity_ledger_id")
    if not ledger_memory_id:
        raise HTTPException(status_code=409, detail="Memory has not been reconciled to a Continuity Ledger ID")
    try:
        result = await engine.continuity.supersede(ledger_memory_id, request.content, request.session_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Continuity Ledger unavailable") from exc
    if result.get("refused") is True or result.get("accepted") is False:
        if result.get("refuse_reason") == "conflict-membrane":
            engine.set_read_only(request.session_id)
        return {
            "status": "conflict" if result.get("refuse_reason") == "conflict-membrane" else "refused",
            "read_only": engine.is_read_only(request.session_id),
            "ledger": result,
        }
    replacement = result.get("memory") or result.get("replacement") or {}
    lineage = result.get("lineage") or replacement.get("lineage") or {}
    if result.get("accepted") is not True or not replacement.get("id"):
        raise HTTPException(status_code=502, detail="Supersession was not confirmed by the Continuity Ledger")
    if replacement.get("id") == ledger_memory_id or (lineage.get("supersedes") not in {None, ledger_memory_id}):
        raise HTTPException(status_code=502, detail="Continuity Ledger returned invalid supersession lineage")
    engine.audit.append(
        f"supersede-{request.memory_id}",
        request.session_id,
        "memory_supersession",
        {
            "memory_id": request.memory_id,
            "ledger_memory_id": ledger_memory_id,
            "subject": "jarvis-prototype",
            "content_sha256": engine.store.content_hash(request.content),
        },
    )
    engine.authorize_session(request.session_id)
    return {"status": "superseded", "read_only": False, "ledger": result}


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, x_jarvis_service_token: str = Header(default="")) -> ChatResponse:
    """Send a message to Jarvis and receive a spiral-aware response."""
    recall_owner = None
    if settings.recall_owner_user_id:
        if not settings.service_token or not secrets.compare_digest(x_jarvis_service_token, settings.service_token):
            raise HTTPException(status_code=401, detail="Operator recall requires a valid service token")
        if request.user_id != settings.recall_owner_user_id:
            raise HTTPException(status_code=403, detail="User ID does not match the server-bound operator")
        recall_owner = settings.recall_owner_user_id
    try:
        return await engine.chat(request, recall_owner=recall_owner)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        raise HTTPException(status_code=500, detail="Chat could not be completed. No response was confirmed.") from None


class ResumeRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)


@router.post("/sessions/resume")
async def resume_session(request: ResumeRequest) -> dict[str, Any]:
    try:
        await engine.get_or_create_session(request.user_id, request.session_id)
        return {
            "state": engine.get_state_summary(request.session_id),
            "memory": engine.get_memory_summary(request.session_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/state/{session_id}")
async def get_state(session_id: str) -> dict[str, Any]:
    """Get the current Jarvis session state including spiral core, emotion, and phase."""
    try:
        return engine.get_state_summary(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/memory/{session_id}")
async def get_memory(session_id: str) -> dict[str, Any]:
    """Get the conversation history and long-term memory for a session."""
    try:
        return engine.get_memory_summary(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/trace")
async def get_trace(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "traces": engine.get_trace(session_id)}


@router.get("/sessions/{session_id}/audit")
async def get_audit(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "events": engine.get_audit(session_id)}


@router.get("/sessions/{session_id}/audit/verify")
async def verify_audit(session_id: str) -> dict[str, Any]:
    return engine.verify_audit(session_id)


@router.delete("/memory/{session_id}")
async def clear_memory(session_id: str) -> dict[str, str]:
    """Clear all memory for a session."""
    try:
        return engine.clear_memory(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
