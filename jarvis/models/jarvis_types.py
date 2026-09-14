"""Jarvis-specific models for the conversational AI layer."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.models.spiral_types import (
    BiofeedbackState,
    IntentMode,
    SpiralCoreState,
)


class SpiralPhase(str, Enum):
    """Pipeline phase label for the current turn (keyword/heuristic, not a reasoning engine)."""

    LISTEN = "listen"
    ORIENT = "orient"
    REASON = "reason"
    RESPOND = "respond"
    REFLECT = "reflect"
    EVOLVE = "evolve"


class SessionLockReason(str, Enum):
    """Why a session refuses new turns. Distinct from governance fail-closed."""

    RECOVERY = "recovery"
    CONFLICT = "conflict"
    VERIFICATION = "verification"


class ChatRequest(BaseModel):
    """Incoming chat message from the user."""

    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=16000)
    session_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    biofeedback: BiofeedbackState | None = None
    input_mode: Literal["text", "voice"] = "text"
    memory_consent: bool = False
    recall_previous: bool = False
    search_query: str | None = Field(default=None, max_length=500)


class ChatResponse(BaseModel):
    """Jarvis response with spiral state metadata."""

    session_id: str
    reply: str
    intent: IntentMode
    phase: SpiralPhase
    energy: float
    confidence: float
    spiral_state: dict[str, float]
    emotion: dict[str, Any]
    memory_snapshot: dict[str, Any]
    reasoning_trace: list[str] = Field(default_factory=list)
    turn_id: str = ""
    decision: str = "answer"
    uncertainty: float = 0.5
    fail_closed_reason: str | None = None
    provider: str = "local"
    model: str = "bounded-local"
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cost_reported: bool = False
    read_only: bool = False
    input_mode: Literal["text", "voice"] = "text"
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list)
    fallback_used: bool = False
    safe_mode: bool = False
    inference_status: str = "not_requested"
    transaction_id: str = ""
    correlation_id: str = ""
    previous_session: dict[str, Any] = Field(default_factory=lambda: {"status": "disabled"})
    context_receipt: dict[str, Any] = Field(default_factory=lambda: {"status": "not_recorded", "citations": []})
    lock_reason: SessionLockReason | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    deliberation: dict[str, Any] = Field(
        default_factory=lambda: {
            "version": "v0-dos-lite",
            "label": (
                "v0 heuristic deliberation pipeline (DOS-lite); not a full DOS Kernel, "
                "trained judge, or private chain-of-thought engine"
            ),
            "status": "not_run",
            "stages": [],
            "evidence": [],
            "claims": [],
            "unsupported_claim_warnings": [],
            "challenge_action": None,
            "challenge_reasons": [],
            "waivers": [],
            "committed": False,
            "blocked_reason": None,
            "response_commit": "committed",
            "memory_admission": "eligible",
            "reply_coverage": {
                "sentence_count": 0,
                "tagged_count": 0,
                "uncovered": [],
                "complete": True,
                "matcher": "v0-token-overlap",
            },
        }
    )
    cer: dict[str, Any] = Field(default_factory=lambda: {"status": "not_recorded"})


class SpiralTurn(BaseModel):
    turn_id: str
    session_id: str
    timestamp: datetime
    decision: str
    uncertainty: float = Field(..., ge=0.0, le=1.0)
    stress: float = Field(..., ge=0.0, le=1.0)
    fail_closed_reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    content: str
    content_sha256: str
    provider: str = "local"
    model: str = "bounded-local"
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    backend_status: str = "standalone"


class JarvisMemoryEntry(BaseModel):
    """A single memory entry in Jarvis's memory system."""

    memory_id: str
    user_id: str
    session_id: str
    content: str
    category: str = "general"
    importance: float = Field(0.5, ge=0.0, le=1.0)
    intent_context: IntentMode = IntentMode.TRANSFORM
    energy_at_capture: float = Field(0.5, ge=0.0, le=1.0)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmotionState(BaseModel):
    """v0 keyword-heuristic emotion scores and label for the current turn."""

    inferred_emotion: str = "calm"
    empathy_mode: str = "balanced"
    urgency: float = Field(0.3, ge=0.0, le=1.0)
    stress: float = Field(0.0, ge=0.0, le=1.0)
    confidence_bias: float = Field(0.0, ge=-0.2, le=0.2)
    rationale: list[str] = Field(default_factory=list)


class JarvisState(BaseModel):
    """Complete state of a Jarvis session."""

    session_id: str
    user_id: str
    phase: SpiralPhase = SpiralPhase.LISTEN
    intent: IntentMode = IntentMode.TRANSFORM
    energy: float = Field(0.5, ge=0.0, le=1.0)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    turn_count: int = 0
    spiral_core: SpiralCoreState = Field(default_factory=SpiralCoreState)
    biofeedback: BiofeedbackState = Field(default_factory=BiofeedbackState)
    emotion: EmotionState = Field(default_factory=EmotionState)
    conversation_history: list[dict[str, str]] = Field(default_factory=list)
    long_term_memory: list[JarvisMemoryEntry] = Field(default_factory=list)
    preferences: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
