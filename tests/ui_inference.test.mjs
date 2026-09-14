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
  assert.match(
    fallbackWaitMessage([
      { provider: "nvidia", model: "nvidia/nemotron-3.5-lightning-30b-a3b" },
      { provider: "nvidia", model: "openai/gpt-oss-20b" },
      { provider: "nvidia", model: "meta/muse-glimmer-30b" }
    ]),
    /Trying nvidia \/ nvidia\/nemotron-3.5-lightning-30b-a3b; fallback nvidia \/ openai\/gpt-oss-20b → nvidia \/ meta\/muse-glimmer-30b/
  );
  assert.doesNotMatch(fallbackWaitMessage([{ provider: "nvidia", model: "primary" }]), /fail-closed|governance/);
});
