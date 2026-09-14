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

v0 implements the first ring only:

1. **Infer cannot reach Commit** without Challenge, Simulate, or a recorded waiver.
2. **Claims carry CRS status and evidence linkage** (Observed / Specified /
   Hypothesized, plus unsupported warnings). A claim is an inspectable
   `{text, tag, evidence_ids}` object, not free-floating prose.
3. **External outputs contribute evidence and cannot establish authority.**
   Tool / RAG / other-agent / `request.context` text is admitted with
   `authority=false`. The runner ignores a requested authority flag.

That attacks three failure modes independently: premature conclusions,
hallucinated certainty, and tool/agent authority. A larger context window
does not substitute for these gates.

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

Challenge can force clarification, abstain, fail-closed, or require more
evidence when existing stress/uncertainty gates or missing citations warrant
it. Fail-closed thresholds stay the same as the governance safety lane
(uncertainty ≥ 0.50, stress > 0.80).

Claim tags are a deterministic sentence/heuristic pass. They are **not**
model-grade NLI and do not prove the hosted LLM used a citation.

## Where it lives

The engine calls the runner during REASON (through Challenge) and again after
the reply exists (Evaluate + Commit). The public envelope is stored on:

- `POST /chat` → `deliberation`
- the turn's audit payload and trace evidence (`type: deliberation`)
- memory-inspection turns (ids and tags only)

Continuity Ledger writes stay gated/disabled unless already enabled. Public
traces omit secrets, hidden prompts, and private reasoning.

## Validation

```sh
poetry run pytest tests/test_deliberation.py -v
```
