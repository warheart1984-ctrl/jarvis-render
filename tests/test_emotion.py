"""Tests for the v0 keyword-heuristic emotion classifier."""

from __future__ import annotations

from jarvis.brain.emotion import infer_emotion
from jarvis.models.spiral_types import BiofeedbackState, EmotionalTone


def test_calm_baseline() -> None:
    result = infer_emotion("Can you help me understand this?")
    assert result.inferred_emotion in {"calm", "curious"}
    assert result.stress < 0.3


def test_frustration_detection() -> None:
    result = infer_emotion("This is broken and I'm frustrated!")
    assert result.stress > 0
    assert any("frustration" in r for r in result.rationale)


def test_urgency_detection() -> None:
    result = infer_emotion("I need this done ASAP, it's urgent!")
    assert result.urgency > 0.2


def test_biofeedback_stress() -> None:
    bio = BiofeedbackState(
        heart_rate=120,
        breath_rate=22,
        voice_intensity=0.9,
        emotional_tone=EmotionalTone.AGITATED,
    )
    result = infer_emotion("Something simple", bio)
    assert result.stress > 0.4
    assert result.empathy_mode == "supportive"


def test_excitement_detection() -> None:
    result = infer_emotion("This is amazing and awesome! I love it!")
    assert result.inferred_emotion == "excited"


def test_build_intent() -> None:
    result = infer_emotion("Let's build and deploy this system now")
    assert result.inferred_emotion == "driven"
