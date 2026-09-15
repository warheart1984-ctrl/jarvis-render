# Security review (seam pass)

Date: 2026-09-15. Scope: auth boundary, CORS, rate limits, readiness,
token vs OAuth, tenant isolation, governed Continuity writes. Findings are from
code review of the current tree, not a penetration test.

## Verdict

Production seams for operator token auth, OAuth visitor sessions, CORS allowlisting,
body size limits, and production governed-write blocking are present and coherent.
No critical auth bypass was found in this pass. Remaining risks are documented
below as accepted operator-model limits or follow-ups.

## Auth and identity

| Control | Status |
| --- | --- |
| Protected routes require service token when `JARVIS_SERVICE_TOKEN` is set or environment is production | Present (`service_boundary` middleware) |
| OAuth mode requires visitor principal or operator token on protected paths | Present |
| Constant-time token compare (`secrets.compare_digest`) | Present |
| Visitor CSRF on mutations | Present (`guard_visitor_mutation` / CSRF header) |
| Supersession requires operator + service token + `user_requested` | Present |
| Propose requires `memory.write` scope under OAuth and explicit `user_requested` | Present |

Operator mode remains a **single shared break-glass credential**, not multi-user
isolation. OAuth visitors are tenant-scoped via `AccessStore`; the service token
must not be embedded in the browser when `JARVIS_AUTH_MODE=oauth`.

## CORS and browser surface

| Control | Status |
| --- | --- |
| Production refuses `*` CORS allowlist | Present (`allowed_cors_origins`) |
| Credentials disabled when origins are `*` | Present |
| UI CSP (`default-src 'self'`, no framing) | Present for `/ui` |
| `Cache-Control: no-store` on protected JSON | Present |
| `X-Content-Type-Options: nosniff` | Present |

## Rate limits and readiness

| Control | Status |
| --- | --- |
| Per-client 60 req/min on protected paths | Present (in-memory; not multi-instance fair) |
| Body size cap (~1 MiB) | Present |
| Visitor daily chat quota | Present |
| `/health/ready` checks local SQLite, not inference quotas | Present |

In-memory rate limits reset per process and are not a substitute for edge WAF
limits on multi-replica deploys.

## Memory / Continuity / supersession

| Control | Status |
| --- | --- |
| Production blocks governed writes even if `JARVIS_GOVERNED_WRITES_ENABLED=true` | Present |
| Propose reconcile only after retrieve hash/ID verification | Present |
| Supersede retries reuse `Idempotency-Key` | Present |
| Conflict membrane sets read-only; unlock only after verified supersession | Present |
| Recovery/verification locks are not cleared by supersession | Present |

Ledger tokens stay server-side under OAuth. Local drafts are not durable authority.

## Tenant isolation notes

SQLite rows carry `tenant_id` / `owner_sub` scope on store and audit paths.
Session ownership checks apply on chat and memory routes. Memory inspection is
owner-scoped and still documents that operator-token inspection is not multi-user
IAM. Do not treat inspection availability as proof of cross-tenant hard isolation
under a compromised operator token.

## Accepted risks / follow-ups

1. Operator service token is omnipotent within the deployment; rotate and store
   only in the platform secret store.
2. In-process rate limiting is best-effort across replicas.
3. EMR production gates remain unimplemented; governed writes stay off in prod.
4. Continuity and Infinity clients trust configured base URLs; pin HTTPS and
   validate certificates in the deployment environment.
5. This review did not include dependency CVE scanning or live Auth0/IdP
   misconfiguration testing.

## Related docs

- [Operations](OPERATIONS.md)
- [Memory provenance](MEMORY_PROVENANCE.md)
- [Chat / voice / recall](CHAT_VOICE.md)
