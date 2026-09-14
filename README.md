# Jarvis

A conversational FastAPI layer that talks to hosted LLMs and optionally syncs
with a [Spiral Intelligence](https://github.com/jhalstead1983-max/NVIDIA) backend.

Each turn runs a local **v0 / heuristic** state loop: a rule-based emotion
classifier, a bounded five-variable spiral-state tracker, and a **DOS-lite**
deliberation pipeline. These are real, tested modules that feed session state,
fail-closed gates, claim tags, and the local fallback responder. They are
**not** a trained emotion model, LLM judgment, computational spiral geometry,
or a full constitutional OS / DOS Kernel.

New memories stay draft; production governed writes remain disabled pending
EMR gates. The same honesty applies here: these engines are useful application
heuristics. Ambition may grow later; this release does **not** claim trained
inference or true spiral math.

## What the local engines actually do

### Emotion classifier (v0 / keyword heuristic)

`infer_emotion` scores the user message against hardcoded keyword lists
(urgency, excitement, frustration, calm, build-intent). Each hit adds about
0.25, capped at 0–1. If/elif thresholds pick a label (`calm`, `curious`,
`driven`, `excited`, `strained`) and an empathy mode.

Optional `BiofeedbackState` fields (heart rate, voice intensity, emotional
tone) can raise stress when a caller supplies them. That is a real interface,
not a live sensor pipeline in this service. Defaults are static placeholders
(`source="manual"`).

The resulting stress and urgency scores gate fail-closed behavior and, on the
local fallback responder, greeting and empathy phrasing. Hosted LLM replies
do not receive these scores as model judgment.

### Spiral-state tracker (v0 / bounded five variables)

`evolve_spiral` is a deterministic state machine. Each turn updates five
clamped scalars — radius, angle, angular_velocity, expansion, coherence —
with fixed increments gated by confidence, stress, emotion, and intent.
Intent-mode switching is a small decision tree, plus a forced round-robin
every five turns.

Angle and radius are tracked metaphors for session motion. They are not
vector geometry feeding downstream math.

Energy uses a deterministic turn-phase signal
(`((turn_count % 7) - 3) / 100`) instead of an earlier unbounded/random
placeholder. Same inputs always produce the same transition.

`determine_phase` picks listen / orient / reason / respond / reflect from
keywords, turn count, and confidence. It is a pipeline label, not a separate
reasoning engine.

### DOS-lite deliberation (v0 / heuristic)

`DeliberationRunner` stages Observe → Interpret → Infer → Challenge (or
Simulate) → Evaluate → Commit. It is a rule list that raises the honesty of a
turn, not Project Finish's full DOS Kernel.

Hard rules: no Commit without an evidence reference (memory, history,
tool/external suggestion, or explicit `none — hypothesized`); Infer cannot
reach Commit without Challenge, Simulate, or a recorded waiver; external /
tool / RAG / other-agent text is evidence, never authority; replies carry
CRS-style Observed / Specified / Hypothesized tags plus unsupported-claim
warnings. See [DOS-lite deliberation](docs/DELIBERATION.md).

## Architecture

```
User  ──▶  Jarvis API (FastAPI :8100)
              │
              ├─ Brain Engine
              │   ├─ Emotion classifier (v0 keyword heuristic + optional BiofeedbackState)
              │   ├─ Spiral-state tracker (v0 five-variable bounded state machine)
              │   ├─ DOS-lite deliberation (v0 Observe→…→Commit heuristic; not a DOS Kernel)
              │   ├─ Responder          (rule-based local fallback; hosted LLM when configured)
              │   └─ Memory Manager     (conversation history + consented draft knowledge)
              │
              └─ Spiral Client SDK ──▶  Spiral Intelligence Backend (:8000)
                                         ├─ V1 Chat
                                         ├─ V7 Memory + Music Engine
                                         └─ V8 Orchestrator + FSM
```

### Turn pipeline

Every message flows through six phases. The names are orchestration labels;
the work inside each step is the v0 heuristics above plus ordinary chat,
memory, and audit code.

1. **LISTEN** — receive the message, resolve the session
2. **ORIENT** — run the keyword emotion classifier, pick a phase label
3. **REASON** — advance the five-variable spiral-state tracker, then run
   DOS-lite Observe → Interpret → Infer → Challenge (or Simulate)
4. **RESPOND** — generate a reply (hosted LLM when configured, else the local
   responder); Evaluate + Commit attach claim tags
5. **REFLECT** — extract memories when consented, update preferences
6. **EVOLVE** — persist the turn; optional Spiral backend sync stays disabled
   in production pending EMR gates

### Intent modes

Jarvis stores the five intent modes from Spiral Intelligence. The local
tracker switches among them with the decision tree described above.

| Mode | Behaviour |
|------|-----------|
| **ascend** | Lift capability — optimistic, forward-moving |
| **destroy** | Challenge assumptions — requires safety gates |
| **expand** | Broaden the solution space |
| **stabilize** | Focus on reliability and safety |
| **transform** | Default — adaptive, balanced evolution |

## Quick Start

For the deployed text and voice console, open `/ui/`. See
[Chat and voice setup](docs/CHAT_VOICE.md) for NVIDIA configuration, API contracts,
microphone controls, recovery behavior, and the deployed smoke test.
The [memory provenance panel](docs/MEMORY_PROVENANCE.md) exposes source IDs,
hashes and per-turn context receipts. New memories stay draft; production
governed writes remain disabled pending EMR gates.

```bash
# Install dependencies
poetry install

# Run the server
poetry run uvicorn jarvis.main:app --host 0.0.0.0 --port 8100 --reload

# Chat with Jarvis
curl -X POST http://localhost:8100/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "jon", "message": "Hello Jarvis, let'\''s build something."}'
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Send a message to Jarvis |
| GET | `/state/{session_id}` | Get session state (spiral tracker, emotion heuristic, phase) |
| GET | `/memory/{session_id}` | Get conversation history + long-term memory |
| DELETE | `/memory/{session_id}` | Clear all memory for a session |
| GET | `/health` | Jarvis health check |
| GET | `/health/spiral` | Spiral Intelligence backend connectivity |

## Configuration

Copy `.env.example` to `.env` and configure:

- `JARVIS_SPIRAL_API_BASE` — URL of the Spiral Intelligence backend
- `JARVIS_LLM_PROVIDER` — `nvidia` for hosted chat, or `mock` for the local responder
- `JARVIS_LLM_API_KEY` — API key for the LLM provider
- `NVIDIA_API_KEY` — NVIDIA hosted API key (server-side only)
- `JARVIS_LLM_MODEL` — defaults to `nvidia/nemotron-3.5-lightning-30b-a3b`
- `JARVIS_SERVICE_TOKEN` — operator token required by protected API routes

## Tests

```bash
poetry run pytest -v
```

`tests/test_emotion.py`, `tests/test_spiral_evolution.py`, and
`tests/test_deliberation.py` cover the v0 heuristics: keyword labels, optional
biofeedback stress, bounded increments, intent-mode gates, the deterministic
energy phase signal, and DOS-lite hard rules (Infer-without-Challenge blocked,
Commit without evidence blocked, recorded waivers, external suggestions as
evidence-only, happy-path Commit with evidence).

## Connecting to Spiral Intelligence

Jarvis works in two modes:

1. **Standalone** — When the Spiral backend is unavailable, Jarvis uses its
   built-in v0 emotion classifier and spiral-state tracker. Conversation,
   heuristic state, and memory features work independently.

2. **Connected** — When the Spiral backend is running and governed writes are
   allowed, Jarvis can sync state with V1 chat, V7 memory, and V8 session
   management. Production governed writes remain disabled pending EMR gates.
   A reachable backend is not a claim that autonomous loops, scoring, or the
   remote policy engine ran inside this turn.

## Project Infinity / EvolveEngine

Project Infinity's bounded evolution service is configured with
`JARVIS_INFINITY_API_BASE` and enabled with `JARVIS_INFINITY_ENABLED=true`.
Jarvis targets the documented `POST /evolve` contract described in
`G:\Project Infinity\docs\contracts\EVOLVE_ENGINE_CONTRACT.md`, including
bounded generation, evaluation, population, and wall-time limits. If the
service is disabled or unavailable, Jarvis remains in its local bounded lane
and does not claim that an external evolution run succeeded.
