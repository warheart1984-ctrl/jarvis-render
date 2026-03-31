"""Chat routes — the main conversational endpoint for Jarvis."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from jarvis.brain.engine import JarvisEngine
from jarvis.models.jarvis_types import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])

engine = JarvisEngine()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send a message to Jarvis and receive a spiral-aware response."""
    try:
        return await engine.chat(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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


@router.delete("/memory/{session_id}")
async def clear_memory(session_id: str) -> dict[str, str]:
    """Clear all memory for a session."""
    try:
        return engine.clear_memory(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
