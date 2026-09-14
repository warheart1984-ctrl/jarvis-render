# Jarvis operations

## Startup

Install with `poetry install`, copy `.env.example` to `.env`, and run
`poetry run uvicorn jarvis.main:app --host 0.0.0.0 --port 8100`.

## Required production settings

Set `JARVIS_ENVIRONMENT=production`, a strong `JARVIS_SERVICE_TOKEN`, an
explicit comma-separated `JARVIS_CORS_ORIGINS` allowlist, and a persistent
`JARVIS_MEMORY_DB_PATH`. Do not use `*` for CORS in production.
`JARVIS_SERVICE_TOKEN` is operator break-glass. Do not put it in the browser
when `JARVIS_AUTH_MODE=oauth`.

## Google sign-in via a managed IdP

Leave `JARVIS_AUTH_MODE=operator` until the IdP is ready. Then set
`JARVIS_AUTH_MODE=oauth` and point Jarvis at Auth0 (or an equivalent with API
audiences and custom scopes). Google is a **social connection on the IdP**,
not Jarvis's authorization server.

Auth0 minimum:

1. Regular Web Application. Allowed callback:
   `https://<host>/auth/callback` and `http://127.0.0.1:8100/auth/callback`.
2. API identifier = `JARVIS_OIDC_AUDIENCE` (the ledger resource). Scopes:
   `memory.read`, `memory.write`. Authorize the web app for both.
3. Social connection: Google. Scopes on Google stay `openid email profile`.
4. Env: `JARVIS_OIDC_ISSUER`, `JARVIS_OIDC_AUDIENCE`, `JARVIS_OIDC_JWKS_URL`,
   `JARVIS_OIDC_CLIENT_ID`, `JARVIS_OIDC_CLIENT_SECRET`,
   `JARVIS_PUBLIC_ORIGIN`, `JARVIS_RECALL_SIGNING_KEY`.
5. `JARVIS_OIDC_CONNECTION=google-oauth2` sends users straight to Google.

The UI keeps an HttpOnly session cookie. The ledger access token stays on the
server and is sent to Continuity only as `Authorization: Bearer` with
`aud` = ledger and both memory scopes. Jarvis does not mint those JWTs.

## Recovery

Each turn is stored with a hash-chained audit event and a verified checkpoint.
On restart, Jarvis restores only a checkpoint whose audit chain and audited turn
anchor verify. Legacy checkpoints do not cryptographically bind their entire saved
state; cross-session recall requires the additional signed attestation described
in [the chat/recall contract](CHAT_VOICE.md#read-only-recall-across-sessions).
Recovered production sessions remain read-only; start a new chat to continue.
Production supersession is blocked until EMR gates are implemented.

## API contracts

`POST /chat` handles conversational turns and returns a public `deliberation`
envelope (v0 / DOS-lite stages, claim tags, unsupported-claim warnings).
`GET /sessions/{id}/trace` exposes decision traces, including the same
DOS-lite envelope, and `/sessions/{id}/audit/verify` verifies the audit chain.
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

## Governance-outcome learning (report-only)

Jarvis can record **per-rule governance check outcomes** into tenant-scoped
SQLite (`governance_outcomes`) and later mine them into an evolution report.
This is **not** Infinity EvolveEngine, not lesson-memory, and not runtime
self-modification. Hard-coded policy lanes stay authoritative until a human
edits code or config.

Outcomes are `violation`, `passed`, `unnecessary`, or `success`. Recording is
observe-only: a check result never changes the current turn's fail-closed
decision. Optional `feedback` text is stored with the row. Hashes from the
existing audit chain are attached when present.

Run a cycle (report-only):

```sh
curl -X POST https://<host>/governance/evolution/run \
  -H "X-Jarvis-Service-Token: $JARVIS_SERVICE_TOKEN"
```

Or in-process: `from jarvis.brain.evolution import run_evolution; run_evolution(store)`.

`GET /governance/evolution` returns the latest tenant report. The memory
inspection payload includes an `evolution` field (`auto_applied` is always
false). `JARVIS_EVOLUTION_ENABLED` defaults to false and does not auto-apply
rules even if set true; it is reserved for an explicit operator cycle, not
silent production policy mutation. `render.yaml` pins it false.
