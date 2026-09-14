import test from "node:test";
import assert from "node:assert/strict";
import { fallbackWaitMessage } from "../jarvis/ui/inference.js";

test("wait copy discloses the fallback sequence and that inference is unconfirmed", () => {
  assert.match(fallbackWaitMessage([]), /not yet confirmed/);
  assert.match(
    fallbackWaitMessage([
      { provider: "nvidia", model: "primary" },
      { provider: "nvidia", model: "backup" }
    ]),
    /Trying nvidia \/ primary; fallback nvidia \/ backup/
  );
  assert.doesNotMatch(fallbackWaitMessage([{ provider: "nvidia", model: "primary" }]), /fail-closed|governance/);
});
