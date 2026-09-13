"""Pydantic models mirroring the Spiral Intelligence V8 backend types."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IntentMode(str, Enum):
    ASCEND = "ascend"
    DESTROY = "destroy"
    EXPAND = "expand"
    STABILIZE = "stabilize"
    TRANSFORM = "transform"


class SessionState(str, Enum):
    IDLE = "idle"
    PRIMED = "primed"
    REHEARSING = "rehearsing"
    PERFORMING = "performing"
    AUTONOMOUS = "autonomous"
    DEGRADED = "degraded"
    ROLLBACK = "rollback"
    COMPLETE = "complete"


class EmotionalTone(str, Enum):
    CALM = "calm"
    FOCUSED = "focused"
    DRIVEN = "driven"
    AGITATED = "agitated"


class BiofeedbackState(BaseModel):
    """Optional caller-supplied signals. Defaults are placeholders, not live sensors."""

    heart_rate: int = 88
    breath_rate: int = 15
    voice_intensity: float = 0.62
    emotional_tone: EmotionalTone = EmotionalTone.FOCUSED
    source: str = "manual"


class SpiralCoreState(BaseModel):
    """v0 five-variable tracker. Angle/radius are metaphors, not geometry inputs."""

    radius: float = 0.58
    angle: float = 180.0
    angular_velocity: float = 0.62
    expansion: float = 0.55
    coherence: float = 0.70


class SessionSnapshot(BaseModel):
    snapshot_id: str
    session_id: str
    user_id: str
    state: SessionState
    prompt: str
    energy: float = Field(..., ge=0.0, le=1.0)
    intent: IntentMode
    biofeedback: BiofeedbackState
    spiral_core: SpiralCoreState
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SessionScore(BaseModel):
    coherence: float
    execution_success: float
    biofeedback_alignment: float
    memory_match: float
    intent_fidelity: float
    reward: float


class EventEnvelope(BaseModel):
    event_id: str
    session_id: str
    user_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class SessionCreateRequest(BaseModel):
    user_id: str
    session_id: str
    prompt: str
    energy: float = Field(0.5, ge=0.0, le=1.0)
    intent: IntentMode = IntentMode.TRANSFORM
    biofeedback: BiofeedbackState = Field(default_factory=BiofeedbackState)
    spiral_core: SpiralCoreState = Field(default_factory=SpiralCoreState)


class SessionExecuteRequest(BaseModel):
    user_id: str
    session_id: str
    prompt: str
    energy: float = Field(..., ge=0.0, le=1.0)
    intent: IntentMode = IntentMode.TRANSFORM
    biofeedback: BiofeedbackState = Field(default_factory=BiofeedbackState)
    spiral_core: SpiralCoreState = Field(default_factory=SpiralCoreState)
    dry_run: bool = True
    osc_host: str = "127.0.0.1"
    osc_port: int = 11000
    output_dir: str = "exports"
    section_name: str = "jarvis_section"
    request_id: str = ""


class SessionTransitionRequest(BaseModel):
    user_id: str
    session_id: str
    to_state: SessionState
    reason: str = ""


class MemoryWriteRequest(BaseModel):
    user_id: str
    session_id: str
    label: str
    energy: float = Field(..., ge=0.0, le=1.0)
    intent: IntentMode
    score: float = Field(..., ge=0.0, le=1.0)
    notes: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class MemoryReadRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    intent: IntentMode | None = None
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
