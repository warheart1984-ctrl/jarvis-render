import test from "node:test";
import assert from "node:assert/strict";
import { lockBanner, LOCK_REASONS } from "../jarvis/ui/locks.js";
import { preserveDraftOnNewChat, readDraft, writeDraft } from "../jarvis/ui/draft.js";

const memory = new Map();
globalThis.localStorage = {
  getItem(key) { return memory.has(key) ? memory.get(key) : null; },
  setItem(key, value) { memory.set(key, String(value)); },
  removeItem(key) { memory.delete(key); }
};

test("recovery, conflict, and verification locks have distinct reason codes", () => {
  assert.equal(lockBanner({ verified: false }).reason, "verification");
  assert.match(lockBanner({ verified: false }).text, /reason=verification/);
  assert.equal(lockBanner({ readOnly: true, lockReason: "recovery", verified: true }).reason, "recovery");
  assert.equal(lockBanner({ readOnly: true, lockReason: "conflict", verified: true }).reason, "conflict");
  assert.doesNotMatch(LOCK_REASONS.recovery, /conflict resolution/);
  assert.doesNotMatch(LOCK_REASONS.conflict, /verified recovery|verification failed/);
  assert.equal(lockBanner({ readOnly: false, verified: true }), null);
});

test("new chat preserves an unsent composer draft", () => {
  memory.clear();
  const kept = preserveDraftOnNewChat("Typed but unsent plan");
  assert.equal(kept, "Typed but unsent plan");
  assert.equal(readDraft(), "Typed but unsent plan");
  writeDraft("");
  assert.equal(readDraft(), "");
});
