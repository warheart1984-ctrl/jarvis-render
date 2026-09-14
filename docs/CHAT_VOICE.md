# Jarvis text and voice

Open the service URL; it now redirects to /ui/. In operator mode, enter a user
ID and the `JARVIS_SERVICE_TOKEN`, then Connect. In OAuth mode the console
shows Sign in with Google; the IdP (Auth0) is the authorization server and
Google is only its social connection. The NVIDIA key belongs only in Render,
never in the browser. Operator tokens stay out of the OAuth UI. The browser
remembers only a session ID and user ID. Conversation history and audit events
are persisted on the server.

Select Voice, press Start recording, speak, and press Stop & send. After
transcription the message is sent through the same /chat endpoint used by text.
Speak replies enables NVIDIA audio output. Stop audio cancels it, and Play reply
replays a previously audited response. Microphone permission and HTTPS are
required. Recording stops automatically within 30 seconds; raw recordings
are held in memory and are not saved to disk.

## Provider configuration

- NVIDIA_API_KEY: the existing hosted NVIDIA account key. When set, current
  NVIDIA NIM chat Nemotron IDs and `meta/muse-glimmer-30b` are appended after
  `JARVIS_LLM_FALLBACK_MODELS`. Missing key skips those slots (degraded chat,
  not a boot failure and not governance fail-closed). NVIDIA is an inference
  backend only; it is not a memory or authority path.
- JARVIS_LLM_PROVIDER=nvidia (a NVIDIA_API_KEY also enables NVIDIA from legacy mock configuration).
- JARVIS_LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
- JARVIS_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
- JARVIS_LLM_MAX_TOKENS=768
- JARVIS_LLM_TIMEOUT_SECONDS=45
- JARVIS_LLM_ATTEMPT_TIMEOUT_SECONDS=15
- JARVIS_LLM_FALLBACK_MODELS=openai/gpt-oss-20b,z-ai/glm-5.3-flash
  Catalog append (when NVIDIA_API_KEY is set):
  nvidia/nemotron-3.5-lightning-30b-a3b (deduped if primary),
  nvidia/nemotron-3-nano-omni-30b-a3b-reasoning,
  nvidia/nemotron-3-super-120b-a12b,
  nvidia/nemotron-3-ultra-550b-a55b,
  meta/muse-glimmer-30b
- JARVIS_SPEECH_VOICE=Magpie-Multilingual.EN-US.Aria
- JARVIS_SERVICE_TOKEN: operator break-glass token. Required in production.
  In operator mode it protects chat, speech, sessions and diagnostics. In
  OAuth mode the UI uses a session cookie instead; keep this token off the page.
- JARVIS_CORS_ORIGINS=https://jarvis-avfy.onrender.com
- JARVIS_MEMORY_DB_PATH=/data/jarvis.sqlite3

Render deploys main. Existing persistent disk and secrets are retained.
NVIDIA text model IDs can retire: former Nano/Mini/Llama-3.1 Nemotron hosted IDs
must not be assumed available. Catalog fallbacks use IDs listed on NVIDIA NIM
at change time (`nemotron-3-nano-omni`, Super, Ultra, Lightning, Muse Glimmer).
Unknown provider costs are labeled not reported, never represented as a confirmed free request.

This is a turn-based speech pipeline (NVIDIA ASR → governed text → NVIDIA TTS).
It is not the full-duplex Nemotron VoiceChat service. The public VoiceChat model
card does not provide a usable hosted transport contract for this deployment.
NVIDIA trial endpoints have account-dependent limits and are not a promise of
unlimited free production hosting. No GPU runs in the Render container.

## API

All endpoints below require `X-Jarvis-Service-Token` in operator mode. In
OAuth mode a signed-in session cookie is enough; the operator token remains
break-glass.

- GET /capabilities: provider/model and configuration presence, no secrets.
- POST /chat: user_id, message, optional session_id, input_mode (text/voice),
  memory_consent and recall_previous (both default false). Reply includes provider/model, latency_ms,
  cost_reported, cost_usd, read_only, decision, uncertainty, turn_id.
- POST /voice/transcribe: raw audio/wav, mono 16-bit PCM, 16 kHz, 0.1–30 seconds;
  returns text/provider/model. No chat turn is generated until /chat is called.
- POST /voice/speak: session_id and turn_id. Returns audio/wav only after verifying
  the saved reply hash and session audit. Arbitrary text synthesis is not exposed.
- POST /sessions/resume: user_id and session_id; restores verified history.
  Recovered sessions remain read-only. New chat creates a fresh session.
- GET /sessions/{session_id}/audit/verify: audit verification.

Inference slots expose accepted, refused, unavailable or unknown outcomes.
The deployed defaults try the primary NVIDIA model, then
`JARVIS_LLM_FALLBACK_MODELS`, then Nemotron + Muse Glimmer catalog IDs when
`NVIDIA_API_KEY` is set. Each attempt takes at most 15 seconds, with a
45-second total budget and a 60-second model cooldown. HTTP 4xx other than 429
is not retried on the same model; 401/403 disable the same credential
reference. An independently configured provider may still answer. A
content/safety refusal is terminal: no alternate model is tried to evade it.

If all inference slots fail, /chat returns a persisted minimal status reply
(HTTP 200 delivery, NOT confirmed inference): safe_mode=true, read_only=true,
decision=degraded, inference_status=unavailable/unknown/refused, provider=internal,
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
Up to eight OpenAI-compatible chat-completion endpoints are supported, including
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

### Inspectable memory and draft writes

The console now includes a Memory provenance panel and per-reply source lists.
See [the memory inspection contract](MEMORY_PROVENANCE.md) for exact IDs/hashes,
optional AMUL references, source-inclusion semantics, and legacy limitations.
New local extracted memories stay draft. Governed external writes are off by
default and blocked in production until EMR gates are implemented.

### Runtime context and long speech

Each model receives server-generated facts about Jarvis (not model self-training),
ten recent messages, and up to eight saved memories from the same user and session.
Saved memory text is bounded to 400 characters per entry and passed as untrusted
user data, never system instructions. Optional cross-session recall is described below.
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

### Read-only recall across sessions

Set `JARVIS_RECALL_OWNER_USER_ID` to the reviewed owner label of this single-operator
installation. The existing `JARVIS_SERVICE_TOKEN` is required. `/capabilities`
returns the configured owner after authentication; the UI binds User ID to it.
`POST /chat` rejects other labels with 403, even if recall is off. A label in a
request is never accepted as an authenticated recall principal. This is NOT
multi-user isolation: anyone with the shared operator token has operator access
to session diagnostics. Use separate authenticated identities before sharing
the service with independent users.

The UI offers **Recall previous conversation (read-only)**, checked by default
when available. API clients opt in with `recall_previous: true`. The server
selects the newest eligible attested checkpoint before the current session was
created, excluding the current session. That cutoff prevents switching sources
mid-conversation. If the selected source has since changed, verification fails;
no older source is silently substituted. A missing eligible source is not proof
that no older conversations exist.

Up to 12 previous user/assistant messages (750 characters each) and four owned
saved memories (400 characters each) are quoted as untrusted user-role data.
The active text provider receives that excerpt, including through configured
fallback slots. The source is read-only: no old turn, memory, checkpoint or audit
event is rewritten and recovered sessions stay locked. Recall does not consent
to new extraction or external writes. Turning recall off stops new retrieval;
it cannot remove information already quoted in the current conversation.

New authenticated turns append an HMAC-SHA256 recall attestation binding owner,
session, checkpoint time, full saved state hash and audit anchor. The HMAC uses
the existing service token with a recall-specific domain separator. Full session
audit verification and an exact checkpoint-state hash match are required on read.
This detects modification without the signing key, not malicious operators with
that key, database deletion/rollback, or compromise of the server itself.
The existing Reviver recovery path still checks its audit anchor; the extra
full-state signature is required for cross-session recall, not retroactively
claimed for all legacy recovery operations.

Legacy sessions are **not automatically trusted or scanned into the model**.
After reviewing exact session IDs and ownership, an operator can explicitly
attest their present contents inside the deployed Render Shell:

```sh
python -m jarvis.persistence.recall REVIEWED_SESSION_ID [ANOTHER_REVIEWED_SESSION_ID]
```

The command reads the existing owner/token/database environment values without
printing secrets. It validates the audit and owner, then appends a recall record;
it is idempotent and never replaces an existing signature. Legacy attestation
establishes integrity from migration onward, not before. After token rotation,
old signatures fail closed until the operator reviews and re-attests the intended
sessions using the new key. Keep the token secret and use persistent `/data` storage.
On Render, merge the new owner variable without replacing existing secrets;
no disk, plan or Blueprint change is needed for this feature.

`previous_session` in each chat response and runtime trace contains `status`
(`verified`, `disabled`, `not_authorized`, `no_eligible_history`, `unverified`,
`unavailable`, or `withheld`). Verified results also identify `source_session_id`,
`checkpoint_id`, `audit_hash`, attestation origin and bounded excerpt counts.
A `previous_session_recall` event records these facts with the current turn's
transaction/correlation IDs before inference; it never duplicates recalled text.
Storage/verification failure supplies no historical text. If recording this
event fails, inference is not called. If a new checkpoint's signature cannot be
saved, the already persisted turn is not presented as confirmed and the session
is locked. Current-turn/checkpoint/signature persistence is not one transaction.

`DELETE /memory/{session_id}` also permanently excludes that source session from
cross-session recall, including after restart or further turns. This is not a
claim of complete historical erasure: existing audit/checkpoint records remain.
Create a new session to begin a new eligible history after clearing one.

### Test commands

Run pytest with the declared dev dependencies and a writable, fresh --basetemp.
On Windows the sandbox ACL can prevent pytest cleanup; run in the normal
user environment using the same isolated test runtime.

Run `node --test tests/ui_audio.test.mjs` with Node 22+ for the browser audio
encoder and playback-cleanup regressions. In restricted environments that block
test subprocesses, use `node --test --test-isolation=none tests/ui_audio.test.mjs tests/ui_recall.test.mjs tests/ui_memory.test.mjs`.

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
