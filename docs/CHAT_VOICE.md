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
- JARVIS_LLM_ATTEMPT_TIMEOUT_SECONDS=15
- JARVIS_LLM_FALLBACK_MODELS=openai/gpt-oss-20b,z-ai/glm-5.3-flash
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

Inference slots expose accepted, refused, unavailable or unknown outcomes.
The deployed defaults use three NVIDIA-hosted models. Each attempt takes at most
15 seconds, with a 45-second total budget and a 60-second model cooldown.
Bad credentials (401/403) disable attempts using that same credential reference;
an independently configured provider may still answer. A content/safety refusal
is terminal: no alternate model is tried to evade it.

If all inference slots fail, /chat returns a persisted minimal status reply
(HTTP 200 delivery, NOT confirmed inference): safe_mode=true, read_only=true,
decision=fail_closed, inference_status=unavailable/unknown/refused, provider=internal,
model=minimal-chat. This is a deterministic status responder, not an offline LLM.
It does not invent answers, extract memories, sync externally, or enable voice.
Storage, authentication or internal programming failures can still return errors.
Availability is conditional on Jarvis and its storage remaining operational.

Every attempted inference is journaled as unknown/started before the network call
and its outcome is journaled afterward. Cooldown skips are logged as not attempted.
Responses, traces and audit events include provider_attempts, transaction_id,
correlation_id, the actual model, fallback_used, safe_mode and inference_status.
Interrupted attempts remain explicitly unknown in the audit. Attempts can consume
provider quota; an unreported total cost is not labeled free.

Speech synthesis never holds the text composer locked. Microphone, transcription
or playback failures switch the console to text mode; existing typed text and
received replies remain available. Safe-mode chat remains usable for basic status
and acknowledges the outage. A failed HTTP request preserves the unsent message;
the browser never silently replays a submitted chat.

## Provider-independent slots

Set JARVIS_LLM_SLOTS to a JSON array to override the legacy provider/model settings.
Up to three OpenAI-compatible chat-completion endpoints are supported, including
llama.cpp on a loopback endpoint. Each slot supports provider, model, base_url,
api_key_env (a server environment-variable NAME), attempts (1–2), timeout_seconds
(1–30) and cooldown_seconds (1–3600). The global time budget still applies.
For example, this single slot uses the existing NVIDIA key without embedding it:

```json
[{"provider":"nvidia","model":"openai/gpt-oss-20b","base_url":"https://integrate.api.nvidia.com/v1","api_key_env":"NVIDIA_API_KEY","attempts":1,"timeout_seconds":15,"cooldown_seconds":60}]
```

All configured slots are trusted server configuration, never request parameters.
HTTPS is required except HTTP to localhost/127.0.0.1/::1. Credentials in URLs are
rejected. Keys are resolved separately per slot and never copied to another provider.
A local slot may omit api_key_env; it then receives no cloud Authorization header.
Non-OpenAI-compatible protocols need an additional adapter; they are not guessed.
No external provider or local LLM is provisioned by this configuration mechanism.

The uncertainty >= 0.5 or stress > 0.8 action gate remains in force.
Low-confidence sessions may use the LLM for read-only discussion/clarification,
without tools, memory extraction or external writes. High stress uses a fixed
clarification. Extracted memory requires explicit consent and an allowed turn.
Conversation/audit persistence still occurs for read-only discussion.
The service token is an operator credential, not multi-user account authentication.

GET /health is liveness. GET /health/ready checks local storage and provider
configuration without spending inference quota; remote connectivity is tested
on requests. It remains ready in explicitly degraded safe mode when inference is
not configured, but returns 503 if local storage is unavailable.

## Verification

### Runtime context and long speech

Each model receives server-generated facts about Jarvis (not model self-training),
ten recent messages, and up to eight saved memories from the same user and session.
Saved memory text is bounded to 400 characters per entry and passed as untrusted
user data, never system instructions. No cross-session lookup is performed.
The trace records the context version, memory counts, consent and policy flags,
but does not duplicate the actual saved memory text in those audit metadata fields.
The prompt distinguishes configuration from connectivity and states that the
optional Infinity hook currently runs after the reply; it does not revise that
reply or retrain the model. It does not claim new integration capabilities.

Responses containing reasoning delimiters or a provider-reported token-limit
truncation are classified as unknown and sent through bounded model fallback.
The rejected output is not presented, spoken or copied into attempt metadata.

The NVIDIA speech error reproduced on 2026-09-13 was an upstream 4 MiB response
limit: a 721-character answer produced a reported 4,792,808-byte response.
Input character limits alone therefore do not ensure successful synthesis.
Jarvis now divides speech into sentence/word-boundary chunks of at most 240
characters (hard-splitting unusually long tokens). It caps each operation at
40 chunks, 50 seconds and 20 MB of assembled audio, with no automatic retries.
Every chunk is audited before and after its request, with the original turn's
transaction/correlation IDs plus the speech request ID. Only validated mono
44.1 kHz 16-bit PCM is assembled into one WAV after all chunks succeed.
Failures never return partial speech; the complete text remains available.
Additional speech events do not replace the checkpoint's audited turn anchor.

NVIDIA documents both the normalized-text limit and the response-size limitation:
[HTTP API](https://docs.nvidia.com/nim/speech/26.07.0/reference/api-references/tts/http-tts.html),
[TTS troubleshooting](https://docs.nvidia.com/nim/speech/26.05.0/troubleshooting/tts.html).

### Test commands

Run pytest with the declared dev dependencies and a writable, fresh --basetemp.
On Windows the sandbox ACL can prevent pytest cleanup; run in the normal
user environment using the same isolated test runtime.

Run `node --test tests/ui_audio.test.mjs` with Node 22+ for the browser audio
encoder and playback-cleanup regressions. In restricted environments that block
test subprocesses, use `node --test --test-isolation=none tests/ui_audio.test.mjs`.

Inside the deployed Render Shell: python -m jarvis.smoke
This creates a synthetic test session and tests auth, two text turns,
speech generation/transcription, voice chat, audio output and audit verification.
It reads existing environment credentials without printing them.

The release regression suite covers provider failure, bounded model fallback,
voice-to-text degradation, audited replies and recovery against locked dependencies.
The dependency audit of every pinned package found no known vulnerabilities
on 2026-09-13. This is not a claim of a complete application security audit.
Authentication checks use the ASGI request path, not a URL reconstructed from
the Host header; malformed-host bypass cases have dedicated regression tests.

Provider contracts:
- https://docs.api.nvidia.com/nim/reference/llm-apis
- https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr/api
- https://build.nvidia.com/nvidia/magpie-tts-multilingual/api
- https://build.nvidia.com/nvidia/nemotron-voicechat/modelcard
