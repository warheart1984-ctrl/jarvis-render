import test from "node:test";
import assert from "node:assert/strict";
import { deliberationSummary, deliberationView, receiptSummary, receiptView, renderInspection } from "../jarvis/ui/memory.js";

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
test("DOS-lite claim tags render as text and do not oversell a kernel", () => {
  const view = deliberationView({
    label: "v0 heuristic deliberation pipeline (DOS-lite); not a full DOS Kernel",
    status: "committed", committed: true, challenge_action: "continue",
    stages: [{name: "observe"}, {name: "infer"}, {name: "challenge"}, {name: "commit"}],
    claims: [{tag: "observed", text: "User utterance observed", unsupported: false},
      {tag: "hypothesized", text: '<img src=x onerror="evil()">', unsupported: true}],
    unsupported_claim_warnings: ["unsupported claim (hypothesized): emotion label"]
  });
  const text = allText(view);
  assert.match(text, /observe → infer → challenge → commit/);
  assert.match(text, /not a full DOS Kernel/);
  assert.match(text, /HYPOTHESIZED/);
  assert.match(text, /<img/);
  assert.match(text, /unsupported claim/);
  assert.equal(deliberationSummary({}), "not run");
});
