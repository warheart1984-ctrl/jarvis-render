# DOS-lite deliberation (v0)

Jarvis runs a **v0 / DOS-lite** deliberation pass on each chat turn. It ports
the useful core of a DOS Kernel-style stage list — Observe → Interpret → Infer
→ Challenge (or Simulate) → Evaluate → Commit — as application heuristics.

This is **not** a full constitutional OS, a trained judge, or a private
chain-of-thought engine. The honesty bar matches the [v0 emotion classifier
and spiral-state tracker](../README.md#what-the-local-engines-actually-do).

## The topology that matters

The useful part is not the stage count. It is a **barrier between forming an
inference and committing to it**, plus an epistemic type on each claim.
Those are runtime checks in `DeliberationRunner.commit` and `EvidenceRef`,
not prompt instructions.

Intended spine (later rings are named here so they are not mistaken for this
release):

```
Deliberation → epistemic discipline → governed evidence → governed memory → continuity
```

v0 implements the first ring, plus a v0 **claim-class gate** on Challenge:

1. **Infer cannot reach Commit** without Challenge, Simulate, or a recorded waiver.
2. **Claims carry CRS status and evidence linkage** (Observed / Specified /
   Hypothesized, plus unsupported warnings). A claim is an inspectable
   `{text, tag, claim_class, support, severity, action, evidence_ids}` object.
   The v0 matcher may link a claim to already-admitted history or memory
   evidence by content-token overlap of the same polarity. Restatement glue
   (“you asked”) is not support, and overlap with a source that negates the
   assertion is not support. Matching is not justification.
3. **External outputs contribute evidence and cannot establish authority.**
   Tool / RAG / other-agent / `request.context` text is admitted with
   `authority=false`. The runner ignores a requested authority flag.
4. **Challenge has teeth by class.** Conversational gaps are optional.
   Interpretive gaps stay hypothesized. Factual gaps qualify. Causal gaps
   revise. Safety-critical gaps block. `require_evidence` plus unsupported
   factual/safety claims cannot `response_commit=committed` with the raw
   assertion treated as supported. Abstain/refuse envelopes must not say
   `committed: true`.
5. **Response commit and memory admission are separate.** Jarvis may say
   “I suspect X” on a qualified/revised response without being allowed to
   store “X is true.”

That attacks premature conclusions, hallucinated certainty, tool/agent
authority, and the inference→memory→truth poison loop. A larger context
window does not substitute for these gates.

## What this release does not do

The fuller loop people remember from older work — Compare, Reflect, CER
lineage/replay, OTEM approve→preview→verify→apply, conflict membranes, and
using deliberation records as evaluation/training material — is **not**
implemented here. Continuity Ledger writes stay gated. Local drafts and
hash-chained audit events are adjacent plumbing, not persistent epistemology
and not a claim that Jarvis learns better deliberation policy between turns.

If those later rings land, they should attach to this same spine rather than
inventing a parallel core.

## What it actually does

`DeliberationRunner` records public stage labels and secret-stripped evidence
refs, then refuses `Commit` when a hard rule fails:

1. No Commit without at least one evidence reference (memory citation, history
   citation, tool/external suggestion marked as evidence, or explicit
   `none — hypothesized`).
2. Infer must be followed by Challenge or Simulate unless a waiver is
   explicitly recorded on the audit/trace.
3. External / tool / RAG / other-agent text is admitted as **evidence, never
   authority** (Voss external-suggestion admission). `request.context` values
   are not copied into public traces.
4. The final envelope carries CRS-style claim tags — Observed / Specified /
   Hypothesized — plus unsupported-claim warnings when a claim lacks evidence.
   Reply sentences are linked to admitted evidence with a v0 token-overlap
   matcher (not NLI): a restatement of the current utterance can be *cited*
   as history without saying “your request”, and a reply that shares
   content words with a memory summary can be cited without naming `memory_id`.
   Matching is not justification. The current utterance is context, not
   automatic support for factual, causal, or safety-critical claims.
   Restatement glue and overlap with a negated source do not count as
   support. Token overlap may identify a candidate citation; it does not
   verify a safety-critical assertion. Whole-answer coverage is a gate: every reply sentence (and each
   240-character window of a longer sentence) must be tagged, or the turn
   cannot `response_commit=committed`. Unchecked text is listed uncovered.
   That is still not proof the hosted model used the citation.

Challenge can force clarification, abstain, fail-closed, qualify, downgrade,
revise, or block. Conversational unsupported claims do not block a turn.
Fail-closed thresholds stay the same as the governance safety lane
(uncertainty ≥ 0.50, stress > 0.80).

Claim tags, classes, and evidence links are a deterministic keyword/heuristic
pass. They are **not** model-grade NLI and do not prove the hosted LLM used a
citation. External text remains evidence, never authority. Linker `match_text`
is used only in-process and is omitted from public traces.

## Where it lives

The engine calls the runner during REASON (through Challenge) and again after
the reply exists (Evaluate + Commit). The public envelope is stored on:

- `POST /chat` → `deliberation`
- the turn's audit payload and trace evidence (`type: deliberation`)
- memory-inspection turns (ids and tags only)

Continuity Ledger writes stay gated/disabled unless already enabled. Public
traces omit secrets, hidden prompts, and private reasoning.

## Observe-only web search (v0)

The first kit tool is **web search in observe-only mode**. It runs only when the
user explicitly asks to search (`search for`, `web search`, `look up`, …) or
sends a gated `search_query` on `POST /chat`. Jarvis does **not** auto-retrieve
for every question.

Hits enter the deliberation corpus as `tool_external` evidence **before Commit**
through the existing Voss admission path (`authority=false`). Citation is not
memory admission: a cited hit does not become a draft or ledger record.

Retrieved text is wrapped in an untrusted-data fence (`channel=untrusted_external_data`,
`instructions=false`, `executable=false`, `memory_eligible=false`) and quoted on
the user channel as data. It is never concatenated into system/governance
prompts as instructions, never treated as a command, never able to change
governance state, and never extracted into memory or preferences.

**Promotion is not shipped.** A later path would require both an explicit user
request and an EMR gate before a cited hit could become draft memory or a
Continuity ledger record. EMR is not implemented. `JARVIS_GOVERNED_WRITES_ENABLED`
stays off. `may_admit_retrieved_to_memory()` returns false even when
`user_requested=true`.

Bounds (numbers, enforced): at most 5 sources, 240-character / 2048-byte
excerpts, 65536-byte provider responses, wall-clock timeout (default 8s),
retry cap 2, **no retry on HTTP 4xx**, per-tenant/session rate limit (8/min),
and host allow/deny lists (default deny `localhost,127.0.0.1,::1,0.0.0.0`).
Every tool uses the same `ToolCallRecord`; missing `transaction_id`,
`correlation_id`, tool name, validated arguments, timeout/retry metadata, or
accepted hashes fail closed. Public traces keep URL, retrieval time, content
hash, bounded excerpt, and trust status `untrusted_external`. They omit
secrets, full pages, chain-of-thought, and `match_text`.

If no search API key is configured, the adapter degrades (`unavailable`) the
same way inference degrades: it is **not** mapped onto governance fail-closed.
A deterministic `fake` backend exists for tests. Calculator, clock, weather,
document retrieval, and health are named stubs on the same tool-call envelope.
This is not unrestricted browse, not RAG, not OTEM, and not NLI.

Claims that use retrieved evidence must cite a search receipt. Unsupported
factual/safety assertions still follow the live-path claim-class teeth
(qualify / block). External text remains evidence, never authority.

## Validation

```sh
poetry run pytest tests/test_deliberation.py tests/test_web_search.py -v
```

