"""Tests for the spiral evolution module."""

from __future__ import annotations

from jarvis.brain.spiral_evolution import determine_phase, evolve_spiral
from jarvis.models.jarvis_types import EmotionState, SpiralPhase
from jarvis.models.spiral_types import IntentMode, SpiralCoreState


def _default_emotion(**overrides) -> EmotionState:
    defaults = {
        "inferred_emotion": "calm",
        "empathy_mode": "balanced",
        "urgency": 0.3,
        "stress": 0.1,
        "confidence_bias": 0.0,
    }
    defaults.update(overrides)
    return EmotionState(**defaults)


def test_spiral_angle_advances() -> None:
    core = SpiralCoreState()
    emotion = _default_emotion()

    new_core, _, _ = evolve_spiral(core, IntentMode.TRANSFORM, 0.5, emotion, 0.6, 1)
    assert new_core.angle != core.angle


def test_high_stress_stabilises_intent() -> None:
    core = SpiralCoreState()
    emotion = _default_emotion(stress=0.8)

    _, new_intent, _ = evolve_spiral(core, IntentMode.EXPAND, 0.5, emotion, 0.6, 1)
    assert new_intent == IntentMode.STABILIZE


def test_coherence_improves_when_calm() -> None:
    core = SpiralCoreState(coherence=0.5)
    emotion = _default_emotion(stress=0.1)

    new_core, _, _ = evolve_spiral(core, IntentMode.TRANSFORM, 0.5, emotion, 0.85, 1)
    assert new_core.coherence > core.coherence


def test_determine_phase_build() -> None:
    phase = determine_phase("Let's build a backend", 0.7, 5)
    assert phase == SpiralPhase.RESPOND


def test_determine_phase_reflect() -> None:
    phase = determine_phase("Why does this work that way?", 0.7, 5)
    assert phase == SpiralPhase.REFLECT


def test_determine_phase_orient_early() -> None:
    phase = determine_phase("Hello there", 0.5, 0)
    assert phase == SpiralPhase.ORIENT


def test_determine_phase_reason_low_confidence() -> None:
    phase = determine_phase("Something interesting", 0.3, 5)
    assert phase == SpiralPhase.REASON


def test_high_stress_low_confidence_triggers_destroy() -> None:
    core = SpiralCoreState()
    emotion = _default_emotion(stress=0.8, inferred_emotion="strained")

    _, new_intent, _ = evolve_spiral(core, IntentMode.TRANSFORM, 0.5, emotion, 0.4, 1)
    assert new_intent == IntentMode.DESTROY


def test_high_stress_high_confidence_stabilises_not_destroys() -> None:
    core = SpiralCoreState()
    emotion = _default_emotion(stress=0.8, inferred_emotion="strained")

    _, new_intent, _ = evolve_spiral(core, IntentMode.TRANSFORM, 0.5, emotion, 0.7, 1)
    assert new_intent == IntentMode.STABILIZE
