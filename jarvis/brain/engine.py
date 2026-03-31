"""Jarvis engine — orchestrates conversation, spiral evolution, memory, and response generation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from jarvis.brain.emotion import infer_emotion
from jarvis.brain.memory import (
    add_long_term_memory,
    add_to_conversation_history,
    extract_memory,
    update_preferences,
)
from jarvis.brain.responder import generate_response
from jarvis.brain.spiral_evolution import determine_phase, evolve_spiral
from jarvis.core.config import settings
from jarvis.models.jarvis_types import (
    ChatRequest,
    ChatResponse,
    JarvisState,
)
from jarvis.spiral_client import SpiralClient

logger = logging.getLogger(__name__)


class JarvisEngine:
    """The main Jarvis orchestrator.

    Manages sessions, processes user messages through the spiral reasoning loop,
    and optionally syncs state with the Spiral Intelligence backend.
    """

    def __init__(self, spiral_client: SpiralClient | None = None) -> None:
        self._sessions: dict[str, JarvisState] = {}
        self._lock = Lock()
        self.spiral = spiral_client or SpiralClient()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def get_or_create_session(self, user_id: str, session_id: str | None = None) -> JarvisState:
        with self._lock:
            if session_id and session_id in self._sessions:
                state = self._sessions[session_id]
                if state.user_id != user_id:
                    raise ValueError("Session does not belong to this user.")
                return state

            new_id = session_id or f"{settings.jarvis_default_session_prefix}-{uuid4().hex[:8]}"
            now = datetime.now(timezone.utc).isoformat()
            state = JarvisState(
                session_id=new_id,
                user_id=user_id,
                energy=settings.default_energy,
                created_at=now,
                updated_at=now,
            )
            self._sessions[new_id] = state
            return state

    def get_session(self, session_id: str) -> JarvisState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _save_session(self, state: JarvisState) -> None:
        with self._lock:
            state.updated_at = datetime.now(timezone.utc).isoformat()
            self._sessions[state.session_id] = state

    # ------------------------------------------------------------------
    # Main conversation loop
    # ------------------------------------------------------------------

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Process a user message through the full Jarvis spiral loop.

        Steps:
        1. LISTEN  — receive the message and resolve the session
        2. ORIENT  — infer emotion, determine phase, detect intent signals
        3. REASON  — evolve spiral state based on emotional and contextual signals
        4. RESPOND — generate a contextual reply
        5. REFLECT — extract memories and update preferences
        6. EVOLVE  — mutate the spiral for the next turn
        """

        state = self.get_or_create_session(request.user_id, request.session_id)

        # --- 1. LISTEN ---
        biofeedback = request.biofeedback or state.biofeedback

        # --- 2. ORIENT ---
        emotion = infer_emotion(request.message, biofeedback)
        state.emotion = emotion
        state.biofeedback = biofeedback

        confidence = max(0.25, min(0.95, state.confidence + emotion.confidence_bias))

        phase = determine_phase(request.message, confidence, state.turn_count)
        state.phase = phase

        # --- 3. REASON ---
        new_core, new_intent, new_energy = evolve_spiral(
            core=state.spiral_core,
            intent=state.intent,
            energy=state.energy,
            emotion=emotion,
            confidence=confidence,
            turn_count=state.turn_count,
        )
        state.spiral_core = new_core
        state.intent = new_intent
        state.energy = new_energy
        state.confidence = confidence

        # --- 4. RESPOND ---
        reply, reasoning_trace = generate_response(state, request.message)

        # --- 5. REFLECT ---
        state.conversation_history = add_to_conversation_history(state, request.message, reply)

        memory_entry = extract_memory(state, request.message, reply)
        if memory_entry:
            state.long_term_memory = add_long_term_memory(state, memory_entry)

        state.preferences = update_preferences(state, request.message)
        state.turn_count += 1

        # --- 6. EVOLVE (sync with Spiral backend if available) ---
        await self._sync_with_spiral(state, request.message, reply)

        self._save_session(state)

        return ChatResponse(
            session_id=state.session_id,
            reply=reply,
            intent=state.intent,
            phase=state.phase,
            energy=state.energy,
            confidence=state.confidence,
            spiral_state=state.spiral_core.model_dump(),
            emotion=state.emotion.model_dump(),
            memory_snapshot={
                "conversation_turns": len(state.conversation_history) // 2,
                "long_term_entries": len(state.long_term_memory),
                "preferences": state.preferences,
            },
            reasoning_trace=reasoning_trace,
        )

    # ------------------------------------------------------------------
    # State retrieval
    # ------------------------------------------------------------------

    def get_state_summary(self, session_id: str) -> dict[str, Any]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        return {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "phase": state.phase.value,
            "intent": state.intent.value,
            "energy": state.energy,
            "confidence": state.confidence,
            "turn_count": state.turn_count,
            "spiral_core": state.spiral_core.model_dump(),
            "emotion": state.emotion.model_dump(),
            "preferences": state.preferences,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }

    def get_memory_summary(self, session_id: str) -> dict[str, Any]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        return {
            "session_id": state.session_id,
            "conversation_history": state.conversation_history,
            "long_term_memory": [m.model_dump() for m in state.long_term_memory],
            "preferences": state.preferences,
        }

    def clear_memory(self, session_id: str) -> dict[str, str]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        state.conversation_history = []
        state.long_term_memory = []
        state.preferences = {}
        self._save_session(state)

        return {"status": "cleared", "session_id": session_id}

    # ------------------------------------------------------------------
    # Spiral backend sync
    # ------------------------------------------------------------------

    async def _sync_with_spiral(self, state: JarvisState, user_message: str, reply: str) -> None:
        """Optionally push state to the Spiral Intelligence backend."""

        try:
            if not await self.spiral.is_available():
                return

            # Push a chat turn to the V1 backend.
            await self.spiral.spiral_chat(
                user_id=state.user_id,
                message=user_message,
                session_id=state.session_id,
            )

            # Write a memory entry to the V7 backend.
            await self.spiral.write_memory(
                user_id=state.user_id,
                session_id=state.session_id,
                label=f"jarvis:{state.intent.value}:{state.turn_count}",
                energy=state.energy,
                intent=state.intent,
                score=state.confidence,
                notes=f"Jarvis turn {state.turn_count}: {user_message[:100]}",
            )
        except Exception as exc:
            logger.debug("Spiral sync skipped: %s", exc)
