import test from "node:test";
import assert from "node:assert/strict";
import { receiptSummary, receiptView, renderInspection } from "../jarvis/ui/memory.js";

// Tiny strict DOM stub: HTML injection and links fail instead of silently passing.
class Node {
  children = []; dataset = {}; textContent = "";
  constructor(tag) { this.tag = tag; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set innerHTML(_) { throw Error("HTML must not be used"); }
  set href(_) { throw Error("External links must not be used"); }
}
globalThis.document = {createElement: tag => new Node(tag)};
const allText = n => [n.textContent, ...n.children.map(allText)].join(" ");
test("receipt statuses never mistake unavailable inference for influence", () => {
  assert.equal(receiptSummary({status: "included", citations: [{}, {}]}), "2 context sources");
  assert.equal(receiptSummary({status: "no_inference"}), "No accepted inference");
  assert.match(receiptSummary(undefined), /Not recorded/);
  assert.match(allText(receiptView({status: "no_inference", citations: [{memory_id: "MUST_NOT_RENDER"}]})), /No source inclusion/);
  assert.doesNotMatch(allText(receiptView({status: "no_inference", citations: [{memory_id: "MUST_NOT_RENDER"}]})), /MUST_NOT_RENDER/);
});
test("citations show real identifiers, hashes and history without fabricated memory IDs", () => {
  const view = receiptView({status: "included", citations: [{source_type: "history", session_id: "s",
    role: "user", citation_id: "c", content_sha256: "a".repeat(64), excerpt_sha256: "b".repeat(64),
    excerpt_chars: 400, truncated: true}]}, "turn-1");
  const text = allText(view);
  assert.match(text, /Not a memory record/); assert.match(text, /not proof of causal influence/);
  assert.match(text, /400 characters · truncated/); assert.ok(text.includes("a".repeat(64)));
});
test("stored content and artifact references are rendered as text only", () => {
  const root = new Node("div");
  renderInspection(root, {status: "available", withheld_records: 0, turns: [], records: [{
    status: "draft", preview: '<img src=x onerror="evil()">', amul_artifact: "javascript:evil()",
    memory_id: "memory-1", session_id: "s", content_sha256: "f".repeat(64), integrity: "audit_bound"}]});
  assert.match(allText(root), /DRAFT/); assert.match(allText(root), /<img/);
  assert.match(allText(root), /not checked by this view/);
  renderInspection(root, {status: "unverified"});
  assert.match(allText(root), /withheld/); assert.doesNotMatch(allText(root), /<img|memory-1/);
});
test("inspection shows deliberation stages and hypothesized claims as unverified", () => {
  const view = receiptView({status: "not_recorded"}, "turn-2", {
    unsupported_claim_warning: "1 hypothesized claim(s) lack a cited source.",
    deliberation: {committed: true, stages: [
      {name: "infer", status: "completed"}, {name: "challenge", status: "completed"},
      {name: "simulate", status: "skipped"}, {name: "commit", status: "completed"}]},
    claims: [{tag: "hypothesized", source: "model", authority: false, text: "Paris is the capital of Mars."}]
  });
  const text = allText(view);
  assert.match(text, /infer:completed → challenge:completed → simulate:skipped → commit:completed/);
  assert.match(text, /evidence, not authority/);
  assert.match(text, /hypothesized claim/);
  assert.match(text, /Paris is the capital of Mars/);
});
test("inspection shows CER replay fields from the existing audit without a research UI", () => {
  const view = receiptView({status: "not_recorded"}, "turn-2", {
    cer: {
      version: "jarvis-cer-v1",
      schema: "constitutional_execution_record",
      model_identity: {provider: "test", model: "model"},
      verification: {challenge: "completed"},
      replay: {input_sha256: "aa", content_sha256: "bb"},
      lineage: {previous_turn_id: "turn-1"}
    }
  });
  const text = allText(view);
  assert.match(text, /existing audit/);
  assert.match(text, /Not a separate ledger/);
  assert.match(text, /test \/ model/);
  assert.match(text, /turn-1/);
  assert.ok(text.includes("aa"));
  assert.ok(text.includes("bb"));
});
