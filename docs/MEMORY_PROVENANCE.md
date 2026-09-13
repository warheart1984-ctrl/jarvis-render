# Inspectable memory

The **Memory provenance** panel at `/ui/` shows Jarvis SQLite records and a
per-turn **Influenced this turn** citation list. This is a read-only inspector,
not a two-way connection between an outside assistant and the Continuity Ledger.
It does not scan or synchronize a separately running Ledger service.

## What a citation means

`POST /chat` returns `context_receipt` with `version`, `status`, `meaning`, and
`citations`. Status `included` means an accepted model request contained the
listed source text. It does **not** prove the model relied on it, that it is true,
or that it is an approved memory. Local development responses, high-stress
clarifications, refused/failed inference, and safe mode have no confirmed
influence citations (`not_requested` or `no_inference`).

Selection occurs while building the actual prompt, before inference. Current
turn extraction happens afterward and cannot appear as a source of that answer.
Fallback providers receive the same context; their attempt logs identify which
provider/model accepted it. The receipt is saved in the turn's `runtime_context`
in both trace evidence and its hash-chained audit event. Inspection reads the
verified audit payload, not the mutable trace JSON. No source content or arbitrary
memory metadata is duplicated in these receipts.

Each source has:

| Field | Meaning |
| --- | --- |
| `citation_id` | SHA-256-derived ID of the source-reference envelope |
| `source_type` | `memory` or `history`; history messages are not memory records |
| `memory_id` | Actual extracted record ID, or `null` for history |
| `session_id` | Source session, not necessarily the current turn's session |
| `content_sha256` | SHA-256 of the full source text encoded as UTF-8, without normalization |
| `excerpt_sha256` | SHA-256 of the exact selected text before JSON prompt serialization |
| `excerpt_chars`, `truncated` | Python Unicode-character count and whether the text was shortened |
| `checkpoint_id` | Signed prior-session checkpoint when applicable; otherwise `null` |
| `message_ref`, `role` | History turn ID, timestamp, or bounded-list index and role |
| `ledger_memory_id` | Optional stored Continuity Ledger reference; not a fresh remote verification |
| `amul_artifact` | Optional stored artifact reference, never inferred or fabricated |

Memory records may carry `metadata.amul_artifact` as a string reference, or an
object with `artifact_id` and optional `content_sha256`. Only those string fields
(at most 512 characters each) are exposed. Artifact references are displayed as
text, not executable links. Presence is labeled **not checked**, absence **not
linked**. This is an optional local metadata convention, not a claimed upstream
AMUL API/schema integration.

## Inspection API and integrity

`GET /sessions/{session_id}/memory-inspection?user_id={owner}` always requires
`X-Jarvis-Service-Token`, including development. The session must be loaded (use
the existing authenticated resume flow after restart), match `user_id`, and, when
configured, match the server-bound `JARVIS_RECALL_OWNER_USER_ID`. This remains a
single-operator credential, **not multi-user identity isolation**. Responses are
`Cache-Control: no-store` and do not call an inference or remote Ledger API.

The response includes `records`, the latest 20 `turns`, `withheld_records`,
`previous_session`, `new_memory_status: draft`, and the governed-write policy.
`read_only: true` describes this endpoint, not whether ordinary chat is locked.
Errors: 401 for missing/invalid credentials, 403 for a mismatched configured
operator, 404 for an unloaded/unowned session, and 503 for an unavailable
inspection dependency. A failed audit/turn hash returns `status: unverified`
with empty records/turns; cleared sessions return `withheld`.

Current-session records must match their stored owner, content and SHA-256.
New records additionally match their creation audit's ID/hash/status/optional
references. Damaged rows are counted as withheld and their content is not shown.
Legacy records are explicitly **legacy/unreviewed**, with `hash_matches_storage`
rather than a retroactive creation attestation. An audit chain verifies internal
integrity, not factual truth or protection against a fully compromised server.

Prior record previews require the exact signed checkpoint used by the latest
turn, independently reverified with the existing recall rules. Only cited
memories from that source are shown. An opted-out, revoked, changed, or invalid
source is not replaced with a different session to fill the panel. Historical
receipts remain evidence of context inclusion at the time, not proof that a source
remains available now. Older turns without receipts say **not recorded**; receipts
are never backfilled by guessing from reply text. New history messages carry
`turn_id` so citations reconnect to replies after reload.

## Drafts and the public write boundary

Conversation/audit storage continues as before. `memory_consent: true` plus an
allowed policy decision can create a **local draft**. New draft rows are saved
atomically with their turn and creation audit; they are not promoted to `active`.
Drafts can still enter later bounded, untrusted conversation context. Consent
does not make their contents approved facts.

`JARVIS_GOVERNED_WRITES_ENABLED` defaults to `false`. It gates `/memory/propose`,
`/memory/supersede`, and external Spiral/Infinity sync. Even setting it to `true`
cannot enable these paths in `production`/`prod`: production remains blocked
until real EMR gates are implemented. There is no browser override or promotion
button. This release does **not** implement EMR. Recovered production sessions
therefore stay read-only; start a new chat to continue with eligible recall.
The development opt-in exists only for explicitly configured integration tests.
Existing local clear-memory behavior is unchanged; it is not complete audit erasure.

## Validation

Run the full Python suite with a fresh writable `--basetemp` and
`-p no:cacheprovider`. `tests/test_memory_inspection.py` covers ownership, draft
persistence, exact excerpt hashes, safe mode/refusal, recovery, tamper withholding,
optional artifact mapping, revoked sources, and production write blocking.

Run UI unit checks with:

```sh
node --test --test-isolation=none tests/ui_audio.test.mjs tests/ui_recall.test.mjs tests/ui_memory.test.mjs
```

Browser verification should use disposable SQLite data and a mocked provider.
Check expanded record IDs/hashes against per-turn citations, phone-width wrapping,
reloaded history, an invalid-token refresh, and New chat clearing prior inspection.
This feature does not require exporting real memories or provider keys for tests.
