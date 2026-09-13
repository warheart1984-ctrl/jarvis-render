export function encodeWav(chunks, sourceRate) {
  const length = chunks.reduce((sum, c) => sum + c.length, 0);
  const input = new Float32Array(length);
  let offset = 0;
  for (const chunk of chunks) { input.set(chunk, offset); offset += chunk.length; }
  const count = Math.min(480000, Math.floor(length * 16000 / sourceRate));
  const buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
  const str = (at, value) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  str(0, "RIFF"); view.setUint32(4, 36 + count * 2, true); str(8, "WAVE"); str(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, "data"); view.setUint32(40, count * 2, true);
  for (let i = 0; i < count; i++) {
    const start = Math.floor(i * sourceRate / 16000), end = Math.max(start + 1, Math.floor((i + 1) * sourceRate / 16000));
    let sum = 0; for (let j = start; j < end && j < length; j++) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / (end - start)));
    view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

export async function createRecorder() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  let context, source, node, gain;
  const chunks = [];
  try {
    context = new AudioContext();
    await context.audioWorklet.addModule("/ui/recorder.js");
    source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "jarvis-recorder");
    gain = context.createGain(); gain.gain.value = 0;
    node.port.onmessage = e => chunks.push(e.data);
    source.connect(node); node.connect(gain); gain.connect(context.destination);
    await context.resume();
  } catch (error) {
    stream.getTracks().forEach(t => t.stop());
    if (context) await context.close();
    throw error;
  }
  let stopped = false;
  return {
    async stop() {
      if (stopped) return null;
      stopped = true;
      stream.getTracks().forEach(t => t.stop());
      source.disconnect(); node.disconnect(); gain.disconnect();
      await context.close();
      return encodeWav(chunks, context.sampleRate);
    }
  };
}
