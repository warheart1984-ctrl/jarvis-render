"""Spiral state evolution — updates Jarvis's spiral core and intent based on conversation flow."""

from __future__ import annotations

import random

from jarvis.models.jarvis_types import EmotionState, SpiralPhase
from jarvis.models.spiral_types import IntentMode, SpiralCoreState


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, round(value, 4)))


def evolve_spiral(
    core: SpiralCoreState,
    intent: IntentMode,
    energy: float,
    emotion: EmotionState,
    confidence: float,
    turn_count: int,
) -> tuple[SpiralCoreState, IntentMode, float]:
    """Evolve the spiral state based on the current conversation context.

    Returns the updated (spiral_core, intent, energy) tuple.
    """

    # Angle always advances — the spiral keeps turning.
    new_angle = (core.angle + 15.0 + energy * 10.0) % 360.0

    # Radius grows with confidence and contracts under stress.
    radius_delta = 0.02 if confidence > 0.7 else -0.01 if emotion.stress > 0.5 else 0.005
    new_radius = clamp(core.radius + radius_delta)

    # Angular velocity responds to urgency.
    vel_delta = 0.03 if emotion.urgency > 0.5 else -0.01
    new_velocity = clamp(core.angular_velocity + vel_delta)

    # Expansion grows when exploring, contracts when stabilising.
    if intent in {IntentMode.EXPAND, IntentMode.ASCEND}:
        expansion_delta = 0.03
    elif intent == IntentMode.STABILIZE:
        expansion_delta = -0.04
    else:
        expansion_delta = 0.01
    new_expansion = clamp(core.expansion + expansion_delta)

    # Coherence improves with consistent intent, drops when stressed.
    coherence_delta = 0.02 if emotion.stress < 0.3 else -0.03
    if confidence > 0.8:
        coherence_delta += 0.02
    new_coherence = clamp(core.coherence + coherence_delta)

    new_core = SpiralCoreState(
        radius=new_radius,
        angle=new_angle,
        angular_velocity=new_velocity,
        expansion=new_expansion,
        coherence=new_coherence,
    )

    # Intent evolution — shifts based on emotional context and turn progression.
    new_intent = _evolve_intent(intent, emotion, confidence, turn_count)

    # Energy nudges toward the emotional centre.
    energy_delta = random.uniform(-0.03, 0.03)
    if emotion.urgency > 0.6:
        energy_delta += 0.04
    if emotion.stress > 0.6:
        energy_delta -= 0.03
    new_energy = clamp(energy + energy_delta)

    return new_core, new_intent, new_energy


def _evolve_intent(
    current: IntentMode,
    emotion: EmotionState,
    confidence: float,
    turn_count: int,
) -> IntentMode:
    """Decide whether to shift the intent mode based on conversation signals."""

    # Under high stress, bias toward stabilisation.
    if emotion.stress > 0.6 and current != IntentMode.STABILIZE:
        return IntentMode.STABILIZE

    # High confidence + driven emotion → ascend.
    if confidence > 0.85 and emotion.inferred_emotion == "driven":
        return IntentMode.ASCEND

    # Excitement → expand.
    if emotion.inferred_emotion == "excited" and current != IntentMode.EXPAND:
        return IntentMode.EXPAND

    # Every 5 turns, consider a natural shift to keep things evolving.
    if turn_count > 0 and turn_count % 5 == 0:
        cycle = [IntentMode.TRANSFORM, IntentMode.EXPAND, IntentMode.ASCEND, IntentMode.STABILIZE]
        idx = cycle.index(current) if current in cycle else 0
        return cycle[(idx + 1) % len(cycle)]

    return current


def determine_phase(
    message: str,
    confidence: float,
    turn_count: int,
) -> SpiralPhase:
    """Decide which spiral phase Jarvis should operate in for this turn."""

    lower = message.lower()

    # Explicit reflection triggers.
    if any(kw in lower for kw in ["why", "explain", "reflect", "meaning", "how does"]):
        return SpiralPhase.REFLECT

    # Early turns → orient and listen.
    if turn_count < 2:
        return SpiralPhase.ORIENT

    # Build / action signals.
    if any(kw in lower for kw in ["build", "code", "make", "create", "deploy", "fix"]):
        return SpiralPhase.RESPOND

    # High confidence → respond decisively.
    if confidence > 0.8:
        return SpiralPhase.RESPOND

    # Low confidence → reason more.
    if confidence < 0.5:
        return SpiralPhase.REASON

    # Default → evolve through reasoning.
    return SpiralPhase.REASON
