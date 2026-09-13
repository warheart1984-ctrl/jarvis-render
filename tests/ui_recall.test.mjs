import test from "node:test";
import assert from "node:assert/strict";
import { recallStatus } from "../jarvis/ui/recall.js";

test("recall status identifies source and never claims perfect memory", () => {
  const text = recallStatus({status: "verified", source_session_id: "old-session", history_messages: 12});
  assert.match(text, /old-session/);
  assert.match(text, /12 history messages/);
  assert.match(text, /bounded excerpt/);
  assert.match(recallStatus({status: "verified", attestation: "operator_attested_legacy"}), /attested by the operator/);
});

test("unavailable, disabled and tampered recall are visibly distinguished", () => {
  assert.match(recallStatus({status: "unverified"}), /not sent/);
  assert.match(recallStatus({status: "unavailable"}), /No prior history/);
  assert.match(recallStatus({status: "disabled"}), /off/);
  assert.match(recallStatus({status: "withheld"}), /cleared/);
  assert.match(recallStatus({status: "no_eligible_history"}), /No eligible/);
  assert.match(recallStatus({status: "unexpected"}), /not been checked/);
});
