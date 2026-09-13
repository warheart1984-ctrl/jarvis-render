# Jarvis

A conversational AI assistant powered by [Spiral Intelligence](https://github.com/jhalstead1983-max/NVIDIA).

Jarvis wraps the Spiral Intelligence V8 backend as its cognitive engine, adding a conversational interface with emotion reasoning, adaptive memory, and spiral state evolution that shapes every response.

## Architecture

```
User  ──▶  Jarvis API (FastAPI :8100)
              │
              ├─ Brain Engine
              │   ├─ Emotion Reasoner   (infers stress, urgency, empathy mode)
              │   ├─ Spiral Evolution   (evolves intent, energy, coherence each turn)
              │   ├─ Responder          (generates spiral-aware replies)
              │   └─ Memory Manager     (conversation history + long-term knowledge)
              │
              └─ Spiral Client SDK ──▶  Spiral Intelligence Backend (:8000)
                                         ├─ V1 Chat
                                         ├─ V7 Memory + Music Engine
                                         └─ V8 Orchestrator + FSM
```

### Spiral Reasoning Loop

Every message flows through six phases:

1. **LISTEN** — receive the message, resolve the session
2. **ORIENT** — infer emotion, determine phase, detect intent signals
3. **REASON** — evolve spiral state (radius, angle, coherence, expansion)
4. **RESPOND** — generate a contextual reply shaped by intent and emotion
5. **REFLECT** — extract memories, update preferences
6. **EVOLVE** — mutate the spiral for the next turn, sync with Spiral backend

### Intent Modes

Jarvis inherits the five intent modes from Spiral Intelligence:

| Mode | Behaviour |
|------|-----------|
| **ascend** | Lift capability — optimistic, forward-moving |
| **destroy** | Challenge assumptions — requires safety gates |
| **expand** | Broaden the solution space |
| **stabilize** | Focus on reliability and safety |
| **transform** | Default — adaptive, balanced evolution |

## Quick Start

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
| GET | `/state/{session_id}` | Get session state (spiral, emotion, phase) |
| GET | `/memory/{session_id}` | Get conversation history + long-term memory |
| DELETE | `/memory/{session_id}` | Clear all memory for a session |
| GET | `/health` | Jarvis health check |
| GET | `/health/spiral` | Spiral Intelligence backend connectivity |

## Configuration

Copy `.env.example` to `.env` and configure:

- `JARVIS_SPIRAL_API_BASE` — URL of the Spiral Intelligence backend
- `JARVIS_LLM_PROVIDER` — `mock` (rule-based) or future LLM integration
- `JARVIS_LLM_API_KEY` — API key for the LLM provider

## Tests

```bash
poetry run pytest -v
```

## Connecting to Spiral Intelligence

Jarvis works in two modes:

1. **Standalone** — When the Spiral backend is unavailable, Jarvis uses its built-in spiral evolution engine. All conversation, emotion, and memory features work independently.

2. **Connected** — When the Spiral backend is running, Jarvis syncs state with V1 chat, V7 memory, and V8 session management. This enables the full Spiral Intelligence feature set including autonomous loops, scoring, and the policy engine.

## Project Infinity / EvolveEngine

Project Infinity's bounded evolution service is configured with
`JARVIS_INFINITY_API_BASE` and enabled with `JARVIS_INFINITY_ENABLED=true`.
Jarvis targets the documented `POST /evolve` contract described in
`G:\Project Infinity\docs\contracts\EVOLVE_ENGINE_CONTRACT.md`, including
bounded generation, evaluation, population, and wall-time limits. If the
service is disabled or unavailable, Jarvis remains in its local bounded lane
and does not claim that an external evolution run succeeded.
