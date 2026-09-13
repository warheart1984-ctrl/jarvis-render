"""v0 keyword-heuristic emotion classifier.

Scores a message against hardcoded keyword lists and optional BiofeedbackState
fields. This is not a trained classifier, LLM judgment, or a live sensor pipeline.
"""

from __future__ import annotations

from jarvis.models.jarvis_types import EmotionState
from jarvis.models.spiral_types import BiofeedbackState

# Keywords that signal different emotional states in user messages.
_URGENCY_KEYWORDS = ["asap", "urgent", "hurry", "fast", "now", "immediately", "critical"]
_EXCITEMENT_KEYWORDS = ["amazing", "awesome", "love", "great", "fantastic", "perfect", "incredible"]
_FRUSTRATION_KEYWORDS = ["broken", "stuck", "failing", "error", "wrong", "frustrated", "annoyed", "damn"]
_CALM_KEYWORDS = ["explain", "help", "understand", "think", "maybe", "consider", "curious"]
_BUILD_KEYWORDS = ["build", "code", "make", "create", "implement", "deploy", "ship", "launch"]


def _keyword_score(text: str, keywords: list[str]) -> float:
    lower = text.lower()
    hits = sum(1 for kw in keywords if kw in lower)
    return min(1.0, hits * 0.25)


def infer_emotion(message: str, biofeedback: BiofeedbackState | None = None) -> EmotionState:
    """Return a v0 heuristic EmotionState from keywords plus optional biofeedback.

    Each keyword hit adds ~0.25 (capped 0–1). If/elif thresholds pick a label.
    Biofeedback (heart rate, voice intensity, emotional tone) is applied only
    when a caller supplies BiofeedbackState; defaults are static placeholders.
    """

    bio = biofeedback or BiofeedbackState()

    # Biofeedback-derived stress
    stress = 0.0
    rationale: list[str] = []

    if bio.heart_rate >= 105:
        stress += 0.25
        rationale.append("elevated heart rate")
    if bio.voice_intensity >= 0.8:
        stress += 0.20
        rationale.append("high voice intensity")
    if bio.emotional_tone.value in {"agitated", "driven"}:
        stress += 0.20
        rationale.append(f"tone={bio.emotional_tone.value}")

    # Text-derived signals
    urgency_score = _keyword_score(message, _URGENCY_KEYWORDS)
    frustration_score = _keyword_score(message, _FRUSTRATION_KEYWORDS)
    excitement_score = _keyword_score(message, _EXCITEMENT_KEYWORDS)
    calm_score = _keyword_score(message, _CALM_KEYWORDS)
    build_score = _keyword_score(message, _BUILD_KEYWORDS)

    if urgency_score > 0:
        stress += urgency_score * 0.15
        rationale.append("urgent language detected")
    if frustration_score > 0:
        stress += frustration_score * 0.20
        rationale.append("frustration signals in message")

    stress = min(1.0, round(stress, 3))
    urgency = min(1.0, round((build_score * 0.3 + urgency_score * 0.4 + stress * 0.3), 3))

    # Determine inferred emotion
    if stress >= 0.5:
        inferred = "strained"
    elif excitement_score > 0.3:
        inferred = "excited"
    elif build_score > 0.3:
        inferred = "driven"
    elif calm_score > 0.3:
        inferred = "curious"
    else:
        inferred = "calm"

    empathy_mode = "supportive" if stress >= 0.5 else "encouraging" if excitement_score > 0.3 else "balanced"

    confidence_bias = round(
        -0.05 if stress >= 0.6 else 0.03 if bio.emotional_tone.value == "focused" else 0.0,
        3,
    )

    if not rationale:
        rationale = ["baseline emotional profile"]

    return EmotionState(
        inferred_emotion=inferred,
        empathy_mode=empathy_mode,
        urgency=urgency,
        stress=stress,
        confidence_bias=confidence_bias,
        rationale=rationale,
    )
