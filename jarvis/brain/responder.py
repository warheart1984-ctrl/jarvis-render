"""Response generation for Jarvis — builds contextual replies using spiral state."""

from __future__ import annotations

from jarvis.models.jarvis_types import EmotionState, JarvisState, SpiralPhase
from jarvis.models.spiral_types import IntentMode


def generate_response(state: JarvisState, user_message: str) -> tuple[str, list[str]]:
    """Generate a Jarvis reply based on the current state and user message.

    Returns (reply_text, reasoning_trace).

    This is a rule-based responder. When an LLM provider is configured,
    the engine will use it instead and fall back to this function.
    """

    trace: list[str] = []
    phase = state.phase
    intent = state.intent
    emotion = state.emotion
    energy = state.energy
    confidence = state.confidence

    trace.append(f"phase={phase.value}, intent={intent.value}, energy={energy:.2f}, confidence={confidence:.2f}")
    trace.append(f"emotion={emotion.inferred_emotion}, empathy={emotion.empathy_mode}, stress={emotion.stress:.2f}")

    # Build the reply based on the current phase and intent.
    reply_parts: list[str] = []

    # Greeting / acknowledgement
    if state.turn_count == 0:
        reply_parts.append(_greeting(emotion))
        trace.append("first-turn greeting added")

    # Phase-specific reasoning
    if phase == SpiralPhase.LISTEN:
        reply_parts.append("I'm listening carefully and mapping your intent.")
        trace.append("listen phase: pure intake")

    elif phase == SpiralPhase.ORIENT:
        reply_parts.append(_orient_response(user_message, intent))
        trace.append("orient phase: shaping direction")

    elif phase == SpiralPhase.REASON:
        reply_parts.append(_reason_response(user_message, intent, confidence))
        trace.append("reason phase: deepening understanding")

    elif phase == SpiralPhase.RESPOND:
        reply_parts.append(_action_response(user_message, intent, energy))
        trace.append("respond phase: concrete output")

    elif phase == SpiralPhase.REFLECT:
        reply_parts.append(_reflect_response(user_message, state))
        trace.append("reflect phase: self-assessment")

    elif phase == SpiralPhase.EVOLVE:
        reply_parts.append("Evolving my approach based on what I've learned from our conversation.")
        trace.append("evolve phase: spiral mutation")

    # Empathy layer
    if emotion.stress > 0.5:
        reply_parts.append("I can sense this is important to you — I'm prioritising clarity and precision.")
        trace.append("empathy: high-stress acknowledgement")

    # Confidence signal
    if confidence > 0.85:
        reply_parts.append(f"My confidence in this direction is high ({confidence:.0%}).")
    elif confidence < 0.45:
        reply_parts.append(
            "I'm still building confidence here — let me know if I should explore a different angle."
        )

    reply = " ".join(reply_parts)
    return reply, trace


def _greeting(emotion: EmotionState) -> str:
    if emotion.inferred_emotion == "excited":
        return "I can feel the energy — let's channel it."
    if emotion.inferred_emotion == "strained":
        return "I'm here. Let's work through this together."
    if emotion.inferred_emotion == "driven":
        return "Ready to build. Let's go."
    return "Good to connect. I'm ready when you are."


def _orient_response(message: str, intent: IntentMode) -> str:
    lower = message.lower()
    if "music" in lower:
        return (
            "I'm mapping your musical intent. The spiral is orienting toward adaptive sound design — "
            "I'll tune the system toward your creative vision."
        )
    if "build" in lower or "code" in lower:
        return (
            f"I'm orienting toward a build cycle with intent '{intent.value}'. "
            "Let me shape the scope, identify dependencies, and prepare a concrete plan."
        )
    return (
        f"I'm orienting around your request with a '{intent.value}' intent. "
        "I'll map the landscape before committing to a direction."
    )


def _reason_response(message: str, intent: IntentMode, confidence: float) -> str:
    if intent == IntentMode.ASCEND:
        return (
            "Reasoning through an ascent pattern — I'm looking for the path that lifts "
            "capability while preserving stability."
        )
    if intent == IntentMode.EXPAND:
        return "Expanding the solution space — evaluating multiple approaches before narrowing down."
    if intent == IntentMode.STABILIZE:
        return "Focusing on stabilisation — finding the safest, most reliable path forward."
    if intent == IntentMode.DESTROY:
        return (
            "Processing a destructive intent carefully. I'll challenge assumptions "
            "and make sure this is the right move before proceeding."
        )
    return (
        f"Running a reasoning pass at {confidence:.0%} confidence. "
        "I'm stress-testing the approach and looking for blind spots."
    )


def _action_response(message: str, intent: IntentMode, energy: float) -> str:
    lower = message.lower()
    if "build" in lower or "code" in lower or "create" in lower:
        return (
            f"Moving to execution at energy {energy:.0%}. I'm producing a concrete implementation "
            "artifact — structured, testable, and ready to iterate on."
        )
    if "deploy" in lower or "ship" in lower or "launch" in lower:
        return (
            "Preparing for deployment. I'll verify all systems, run a final check, "
            "and push toward launch."
        )
    return (
        f"Acting with '{intent.value}' intent at energy {energy:.0%}. "
        "I'm producing the most useful output I can for this turn."
    )


def _reflect_response(message: str, state: JarvisState) -> str:
    turn = state.turn_count
    coherence = state.spiral_core.coherence
    return (
        f"Reflecting after {turn} turns. Current coherence is {coherence:.2f}. "
        "The system needs memory, self-scoring, and mode switching so each loop "
        "changes future behaviour — that's what makes this feel alive rather than scripted."
    )
