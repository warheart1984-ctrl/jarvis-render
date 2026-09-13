"""Memory management for Jarvis — short-term conversation + long-term extracted knowledge."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from jarvis.core.config import settings
from jarvis.models.jarvis_types import JarvisMemoryEntry, JarvisState


def extract_memory(
    state: JarvisState,
    user_message: str,
    reply: str,
) -> JarvisMemoryEntry | None:
    """Decide whether this turn is worth remembering long-term and extract a memory entry.

    Returns None if the turn is not significant enough to store.
    """

    lower = user_message.lower()
    category = "general"
    importance = 0.4

    # Topic detection
    if any(kw in lower for kw in ["build", "code", "backend", "system", "deploy"]):
        category = "build_intent"
        importance = 0.7
    elif any(kw in lower for kw in ["music", "sound", "ableton", "midi", "osc"]):
        category = "music"
        importance = 0.65
    elif any(kw in lower for kw in ["spiral", "evolve", "intelligence", "ai", "agi"]):
        category = "spiral_intelligence"
        importance = 0.75
    elif any(kw in lower for kw in ["remember", "always", "prefer", "never", "want"]):
        category = "preference"
        importance = 0.8
    elif len(user_message.split()) < 5:
        # Very short messages are usually not worth storing.
        return None

    # Boost importance if confidence is high.
    if state.confidence > 0.8:
        importance = min(1.0, importance + 0.1)

    content = f"User said: {user_message[:200]}"
    if category == "preference":
        content = f"Preference: {user_message[:200]}"

    return JarvisMemoryEntry(
        memory_id=str(uuid4()),
        user_id=state.user_id,
        session_id=state.session_id,
        content=content,
        category=category,
        importance=importance,
        intent_context=state.intent,
        energy_at_capture=state.energy,
        created_at=datetime.now(timezone.utc).isoformat(),
        metadata={"status": "draft"},
    )


def update_preferences(state: JarvisState, user_message: str) -> dict[str, str]:
    """Scan the user message for preference signals and return updated preferences."""

    lower = user_message.lower()
    prefs = dict(state.preferences)

    if "music" in lower:
        prefs["interest_music"] = "true"
    if "spiral" in lower:
        prefs["interest_spiral_modeling"] = "true"
    if any(kw in lower for kw in ["build", "code", "backend", "system"]):
        prefs["prefers_concrete_builds"] = "true"
    if any(kw in lower for kw in ["fast", "quick", "hurry"]):
        prefs["prefers_speed"] = "true"
    if any(kw in lower for kw in ["explain", "why", "understand"]):
        prefs["prefers_explanation"] = "true"

    return prefs


def add_to_conversation_history(
    state: JarvisState,
    user_message: str,
    reply: str,
) -> list[dict[str, str]]:
    """Add the turn to conversation history, keeping it bounded."""

    history = list(state.conversation_history)
    now = datetime.now(timezone.utc).isoformat()

    history.append({"role": "user", "content": user_message, "timestamp": now})
    history.append({"role": "assistant", "content": reply, "timestamp": now})

    max_history = settings.max_conversation_history
    if len(history) > max_history:
        history = history[-max_history:]

    return history


def add_long_term_memory(
    state: JarvisState,
    entry: JarvisMemoryEntry,
) -> list[JarvisMemoryEntry]:
    """Add a memory entry to long-term memory, keeping it bounded."""

    memories = list(state.long_term_memory)
    memories.append(entry)

    max_mem = settings.max_long_term_memory
    if len(memories) > max_mem:
        # Keep the most important memories.
        memories.sort(key=lambda m: m.importance, reverse=True)
        memories = memories[:max_mem]

    return memories
