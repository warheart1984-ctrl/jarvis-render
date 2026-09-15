import test from "node:test";
import assert from "node:assert/strict";
import { lockBanner, LOCK_REASONS } from "../jarvis/ui/locks.js";
import {
  clearDraftsOnLogout,
  composerDraftAfterAuth,
  draftStorageKey,
  preserveDraftOnNewChat,
  readDraft,
  writeDraft
} from "../jarvis/ui/draft.js";

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
  assert.match(LOCK_REASONS.conflict, /supersession|conflict panel/i);
  assert.equal(lockBanner({ readOnly: false, verified: true }), null);
});

test("new chat preserves an unsent composer draft for the same account", () => {
  memory.clear();
  const kept = preserveDraftOnNewChat("alice", "Typed but unsent plan");
  assert.equal(kept, "Typed but unsent plan");
  assert.equal(readDraft("alice"), "Typed but unsent plan");
  assert.equal(memory.get("jarvis.composer_draft"), undefined);
  assert.equal(memory.get(draftStorageKey("alice")), "Typed but unsent plan");
  writeDraft("alice", "");
  assert.equal(readDraft("alice"), "");
});

test("composer drafts are account-scoped and are not loaded before authentication", () => {
  memory.clear();
  memory.set("jarvis.composer_draft", "LEGACY_SHARED_LEAK");
  writeDraft("alice", "Alice unsent invite text");
  writeDraft("bob", "Bob unsent invite text");
  assert.equal(draftStorageKey("alice"), "jarvis.composer_draft.alice");
  assert.notEqual(draftStorageKey("alice"), "jarvis.composer_draft");
  assert.equal(readDraft(""), "");
  assert.equal(readDraft(undefined), "");
  assert.equal(composerDraftAfterAuth({ authenticated: false, userId: "alice" }), "");
  assert.equal(composerDraftAfterAuth({ authenticated: true, userId: "" }), "");
  assert.equal(composerDraftAfterAuth({ authenticated: true, userId: "alice" }), "Alice unsent invite text");
  assert.equal(composerDraftAfterAuth({ authenticated: true, userId: "bob" }), "Bob unsent invite text");
  assert.notEqual(readDraft("alice"), readDraft("bob"));
  assert.equal(memory.get("jarvis.composer_draft"), "LEGACY_SHARED_LEAK");
});

test("logout clears the previous account draft so the next account cannot see it", () => {
  memory.clear();
  memory.set("jarvis.composer_draft", "LEGACY_SHARED_LEAK");
  writeDraft("alice", "Alice unsent invite text");
  writeDraft("bob", "Bob unsent invite text");
  clearDraftsOnLogout("alice");
  assert.equal(readDraft("alice"), "");
  assert.equal(memory.get("jarvis.composer_draft"), undefined);
  assert.equal(composerDraftAfterAuth({ authenticated: false, userId: "bob" }), "");
  assert.equal(composerDraftAfterAuth({ authenticated: true, userId: "bob" }), "Bob unsent invite text");
  assert.notEqual(composerDraftAfterAuth({ authenticated: true, userId: "bob" }), "Alice unsent invite text");
});
