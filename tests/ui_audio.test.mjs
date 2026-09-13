import test from "node:test";
import assert from "node:assert/strict";
import { disposePlayback, encodeWav } from "../jarvis/ui/audio.js";

test("normal playback cleanup cannot fire the voice-failure callback", () => {
  const operations = [];
  const audio = {
    onended: () => assert.fail("stale ended callback"),
    onerror: () => assert.fail("cleanup must not report a playback failure"),
    pause() { operations.push("pause"); },
    removeAttribute(name) {
      assert.equal(this.onended, null);
      assert.equal(this.onerror, null);
      operations.push("remove " + name);
      this.onerror?.();
    },
    load() { operations.push("load"); this.onerror?.(); }
  };
  disposePlayback(audio);
  assert.deepEqual(operations, ["pause", "remove src", "load"]);
  disposePlayback(null);
});

test("recording encoder emits bounded mono 16 kHz PCM WAV", async () => {
  const wav = encodeWav([new Float32Array(48000).fill(0.5)], 48000);
  const data = new DataView(await wav.arrayBuffer());
  assert.equal(wav.type, "audio/wav");
  assert.equal(wav.size, 32044);
  assert.equal(data.getUint32(24, true), 16000);
  assert.equal(data.getUint16(22, true), 1);
  assert.equal(data.getUint16(34, true), 16);
  assert.equal(data.getInt16(44, true), 16383);
  assert.equal(encodeWav([new Float32Array(16000 * 31)], 16000).size, 960044);
});
