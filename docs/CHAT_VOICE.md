# Jarvis text and voice

Open the service URL; it now redirects to /ui/. Enter a user ID and the
JARVIS_SERVICE_TOKEN in the console, then Connect. The NVIDIA key belongs only
in Render, never in the browser. Tokens are held in page memory, not localStorage.
The browser remembers only a session ID and user ID. Conversation history and
audit events are persisted on the server.

Select Voice, press Start recording, speak, and press Stop & send. After
transcription the message is sent through the same /chat endpoint used by text.
Speak replies enables NVIDIA audio output. Stop audio cancels it, and Play reply
replays a previously audited response. Microphone permission and HTTPS are
required. Recording stops automatically within 30 seconds; raw recordings
are held in memory and are not saved to disk.

## Provider configuration

- NVIDIA_API_KEY: the existing hosted NVIDIA account key.
- JARVIS_LLM_PROVIDER=nvidia (a NVIDIA_API_KEY also enables NVIDIA from legacy mock configuration).
- JARVIS_LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
- JARVIS_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
- JARVIS_LLM_MAX_TOKENS=768
- JARVIS_LLM_TIMEOUT_SECONDS=45
- JARVIS_SPEECH_VOICE=Magpie-Multilingual.EN-US.Aria
- JARVIS_SERVICE_TOKEN: the operator token protecting chat, speech, sessions and diagnostics.
- JARVIS_CORS_ORIGINS=https://jarvis-avfy.onrender.com
- JARVIS_MEMORY_DB_PATH=/data/jarvis.sqlite3

Render deploys main. Existing persistent disk and secrets are retained.
NVIDIA text model IDs can retire: the former Nano and Super IDs must not be
assumed available. The current model was tested from the Render service.
Unknown provider costs are labeled not reported, never represented as a confirmed free request.

This is a turn-based speech pipeline (NVIDIA ASR → governed text → NVIDIA TTS).
It is not the full-duplex Nemotron VoiceChat service. The public VoiceChat model
card does not provide a usable hosted transport contract for this deployment.
NVIDIA trial endpoints have account-dependent limits and are not a promise of
unlimited free production hosting. No GPU runs in the Render container.

## API

All endpoints below require X-Jarvis-Service-Token.

- GET /capabilities: provider/model and configuration presence, no secrets.
- POST /chat: user_id, message, optional session_id, input_mode (text/voice),
  memory_consent (defaults false). Reply includes provider/model, latency_ms,
  cost_reported, cost_usd, read_only, decision, uncertainty, turn_id.
- POST /voice/transcribe: raw audio/wav, mono 16-bit PCM, 16 kHz, 0.1–30 seconds;
  returns text/provider/model. No chat turn is generated until /chat is called.
- POST /voice/speak: session_id and turn_id. Returns audio/wav only after verifying
  the saved reply hash and session audit. Arbitrary text synthesis is not exposed.
- POST /sessions/resume: user_id and session_id; restores verified history.
  Recovered sessions remain read-only. New chat creates a fresh session.
- GET /sessions/{session_id}/audit/verify: audit verification.

Provider failures return 503, with no fabricated successful fallback. Errors
are sanitized; the browser preserves the unsent message and never retries a
chat automatically. Speech failures leave the text response available.

The uncertainty >= 0.5 or stress > 0.8 action gate remains in force.
Low-confidence sessions may use the LLM for read-only discussion/clarification,
without tools, memory extraction or external writes. High stress uses a fixed
clarification. Extracted memory requires explicit consent and an allowed turn.
Conversation/audit persistence still occurs for read-only discussion.
The service token is an operator credential, not multi-user account authentication.

GET /health is liveness. GET /health/ready checks local storage and provider
configuration without spending inference quota; remote connectivity is tested
on requests. This distinction is exposed in its JSON.

## Verification

Run pytest with the declared dev dependencies and a writable, fresh --basetemp.
On Windows the sandbox ACL can prevent pytest cleanup; run in the normal
user environment using the same isolated test runtime.

Inside the deployed Render Shell: python -m jarvis.smoke
This creates a synthetic test session and tests auth, two text turns,
speech generation/transcription, voice chat, audio output and audit verification.
It reads existing environment credentials without printing them.

The release regression suite passes 93 tests against the locked dependencies.
The dependency audit of every pinned package found no known vulnerabilities
on 2026-09-13. This is not a claim of a complete application security audit.
Authentication checks use the ASGI request path, not a URL reconstructed from
the Host header; malformed-host bypass cases have dedicated regression tests.

Provider contracts:
- https://docs.api.nvidia.com/nim/reference/llm-apis
- https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr/api
- https://build.nvidia.com/nvidia/magpie-tts-multilingual/api
- https://build.nvidia.com/nvidia/nemotron-voicechat/modelcard
