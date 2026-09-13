# Jarvis operations

## Startup

Install with `poetry install`, copy `.env.example` to `.env`, and run
`poetry run uvicorn jarvis.main:app --host 0.0.0.0 --port 8100`.

## Required production settings

Set `JARVIS_ENVIRONMENT=production`, a strong `JARVIS_SERVICE_TOKEN`, an
explicit comma-separated `JARVIS_CORS_ORIGINS` allowlist, and a persistent
`JARVIS_MEMORY_DB_PATH`. Do not use `*` for CORS in production.

## Recovery

Each turn is stored with a hash-chained audit event and a verified checkpoint.
On restart, Jarvis restores only a checkpoint whose audit chain and audited turn
anchor verify. Legacy checkpoints do not cryptographically bind their entire saved
state; cross-session recall requires the additional signed attestation described
in [the chat/recall contract](CHAT_VOICE.md#read-only-recall-across-sessions).
Recovered production sessions remain read-only; start a new chat to continue.
Production supersession is blocked until EMR gates are implemented.

## API contracts

`POST /chat` handles conversational turns. `GET /sessions/{id}/trace` exposes
decision traces, and `/sessions/{id}/audit/verify` verifies the audit chain.
Consented local memory extraction creates drafts. Governed external writes are
disabled by default and always blocked in production pending EMR gates.
See [memory provenance](MEMORY_PROVENANCE.md) for the authenticated inspection
endpoint, citation contract, draft lifecycle, and write boundary.

## Deployment

The included `Dockerfile` runs the API and stores SQLite under `/data`.
`render.yaml` provisions a persistent Render disk and uses `/health/ready` for
readiness checks. Supply secrets through the deployment platform, not source.

## Project Infinity integration

The local bounded engine is an offline fallback. Replacing it with Project
Infinity requires the backend's authenticated endpoint and request/response
schema; no undocumented wire contract is assumed here.
