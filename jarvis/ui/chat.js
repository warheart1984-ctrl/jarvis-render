import { createRecorder, disposePlayback } from "./audio.js";
import { recallStatus } from "./recall.js";
import { deliberationView, receiptView, renderInspection } from "./memory.js";

const $ = id => document.getElementById(id);
let session = "", connected = false, busy = false, recovered = false;
let recording = null, timer = null, recordingStart = false, caps = {};
let playback = null, audioUrl = null, speechAbort = null, speechGeneration = 0;
let governanceGeneration = 0, csrf = "", authMode = "operator";
function clearInspection() {
  renderInspection($("memory-inspection"), null);
  $("audit-state").textContent = "Audit verification is not current. Refresh after reconnecting.";
  $("recall-state").textContent = "Recall source verification is not current.";
  for (const node of $("messages").querySelectorAll(".context-receipt")) {
    node.replaceWith(receiptView({status: "unavailable"}, node.dataset.turnId));
  }
}
const storage = {
  read() { try { return JSON.parse(localStorage.getItem("jarvis.active") || "null"); } catch { return null; } },
  save() { try { localStorage.setItem("jarvis.active", JSON.stringify({ session, user: $("user-id").value })); } catch {} },
  clear() { try { localStorage.removeItem("jarvis.active"); localStorage.removeItem("jarvis.session"); } catch {} }
};
const active = storage.read();
if (active?.user) $("user-id").value = active.user;
function error(message = "") { $("error").textContent = message; $("error").hidden = !message; }
function activity(message) { $("activity").textContent = message; }
function textMode(reason) {
  $("mode").value = "text"; $("mic").hidden = true; $("voice-note").hidden = true;
  $("spoken").checked = false; stopAudio();
  error(reason + " Switched to text mode; you can keep typing."); activity("Text chat ready.");
}
function controls() {
  const locked = busy || !!recording || recordingStart;
  $("send").disabled = !connected || locked || recovered;
  $("message").disabled = !connected || locked || recovered;
  $("mic").disabled = !connected || busy || recordingStart || recovered || !caps.speech_configured;
  $("new-chat").disabled = locked;
  $("connect").disabled = locked;
  $("mode").disabled = locked;
  $("refresh").disabled = !connected || busy || !session;
  $("consent").disabled = locked || recovered;
  $("recall").disabled = locked || recovered || !connected || !caps.recall_configured;
  $("user-id").disabled = connected;
}
async function request(path, options = {}, audio = false) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  const external = options.signal;
  const abort = () => controller.abort();
  external?.addEventListener("abort", abort, { once: true });
  if (external?.aborted) controller.abort();
  try {
    const headers = { ...options.headers };
    const token = $("token").value.trim();
    if (token) headers["X-Jarvis-Service-Token"] = token;
    if (csrf) headers["X-Jarvis-CSRF"] = csrf;
    const response = await fetch(path, { ...options, headers, signal: controller.signal, cache: "no-store", credentials: "same-origin" });
    if (!response.ok) {
      let detail; try { detail = (await response.json()).detail; } catch {}
      if (response.status === 401) {
        governanceGeneration++; clearInspection();
        connected = false; controls();
        throw Error(authMode === "oauth"
          ? "Session expired or sign-in required. Sign in with Google and try again."
          : "Service token not accepted. Enter your Jarvis token and reconnect.");
      }
      throw Error(typeof detail === "string" ? detail : "Request failed (HTTP " + response.status + ").");
    }
    return audio ? response.blob() : response.json();
  } catch (e) {
    if (e.name === "AbortError") throw Error("Request stopped or timed out. It was not automatically retried.");
    throw e;
  } finally { clearTimeout(timeout); external?.removeEventListener("abort", abort); }
}
const post = (path, data, options = {}, audio = false) => request(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data), ...options
}, audio);
function stopAudio() {
  speechGeneration++;
  speechAbort?.abort(); speechAbort = null;
  disposePlayback(playback); playback = null;
  if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
  $("stop-audio").disabled = true;
}
async function speak(turn) {
  stopAudio();
  error();
  const generation = speechGeneration;
  speechAbort = new AbortController();
  $("stop-audio").disabled = false;
  activity("Preparing spoken reply…");
  try {
    const blob = await post("/voice/speak", { session_id: session, turn_id: turn }, { signal: speechAbort.signal }, true);
    if (generation !== speechGeneration) return;
    audioUrl = URL.createObjectURL(blob); playback = new Audio(audioUrl);
    playback.onended = () => {
      if (generation !== speechGeneration) return;
      stopAudio(); activity("Ready.");
    };
    playback.onerror = () => {
      if (generation !== speechGeneration) return;
      const code = playback?.error?.code;
      textMode("Audio could not play" + (code ? " (media error " + code + ")" : "") + ". Your text reply is still available.");
    };
    await playback.play(); activity("Speaking…");
  } catch (e) {
    if (generation !== speechGeneration) return;
    textMode("Speech playback: " + e.message);
  }
}
function message(role, text, response = null, turnId = "") {
  $("empty")?.remove();
  const article = document.createElement("article"); article.className = "message " + role;
  const label = document.createElement("div"); label.className = "message-label"; label.textContent = role === "user" ? "You" : "Jarvis";
  const body = document.createElement("div"); body.className = "message-content"; body.textContent = text;
  article.append(label, body);
  if (response) {
    const meta = document.createElement("div"); meta.className = "message-meta";
    meta.textContent = response.provider + " · " + response.model
      + (response.safe_mode ? " · SAFE MODE · no inference confirmed" : response.fallback_used ? " · backup model" : "")
      + (response.read_only ? " · read-only discussion" : "");
    article.append(meta);
    const replay = document.createElement("button"); replay.type = "button"; replay.className = "replay secondary";
    replay.textContent = "Play reply"; replay.disabled = !caps.speech_configured || response.safe_mode;
    replay.onclick = () => speak(response.turn_id); article.append(replay);
  }
  if (role !== "user") {
    article.append(receiptView(response?.context_receipt, response?.turn_id || turnId));
    if (response?.deliberation) article.append(deliberationView(response.deliberation));
  }
  $("messages").append(article); article.scrollIntoView({ block: "nearest" });
}
function decision(d) {
  $("decision").textContent = d.read_only
    ? "Read-only discussion. Consequential actions are paused: " + (d.fail_closed_reason || "policy restriction")
    : "Response completed under Jarvis policy"
      + (d.deliberation?.committed ? " after a v0 DOS-lite deliberation commit." : ".");
  $("confidence").textContent = Number(d.confidence).toFixed(2);
  $("uncertainty").textContent = Number(d.uncertainty).toFixed(2);
  $("provider").textContent = d.provider + " / " + d.model;
  $("latency").textContent = Math.round(d.latency_ms) + " ms";
  $("cost").textContent = d.cost_reported ? "$" + Number(d.cost_usd).toFixed(6) : "Not reported by provider";
  $("recall-state").textContent = recallStatus(d.previous_session);
}
function setSession(id) { session = id; $("session-label").textContent = id || "New conversation"; storage.save(); }
async function governance() {
  if (!session) return;
  const generation = ++governanceGeneration;
  const id = encodeURIComponent(session);
  let results;
  try { results = await Promise.all([
    request("/state/" + id), request("/sessions/" + id + "/audit"),
    request("/sessions/" + id + "/trace"), request("/sessions/" + id + "/audit/verify"),
    request("/sessions/" + id + "/memory-inspection?user_id=" + encodeURIComponent($("user-id").value.trim()))
  ]); } catch (e) { if (generation === governanceGeneration) clearInspection(); throw e; }
  if (generation !== governanceGeneration || id !== encodeURIComponent(session)) return;
  const [s, a, t, v, inspection] = results;
  renderInspection($("memory-inspection"), inspection);
  const receipts = new Map(inspection.turns.map(t => [t.turn_id, t.context_receipt]));
  for (const node of $("messages").querySelectorAll(".context-receipt")) {
    node.replaceWith(receiptView(inspection.status === "available"
      ? receipts.get(node.dataset.turnId) : {status: "unavailable"}, node.dataset.turnId));
  }
  $("state").textContent = JSON.stringify(s, null, 2); $("trace").textContent = JSON.stringify(t, null, 2);
  $("audit").textContent = JSON.stringify(a, null, 2);
  const lastTurn = [...a.events].reverse().find(e => e.event_type === "spiral_turn");
  if (lastTurn) {
    try { $("recall-state").textContent = recallStatus(JSON.parse(lastTurn.payload_json).runtime_context?.previous_session); }
    catch { $("recall-state").textContent = "Recall metadata could not be read."; }
  }
  $("memory-state").textContent = s.memory_count + " extracted memories. External storage is not confirmed by this count.";
  const verified = v.valid && inspection.status !== "unverified";
  $("audit-state").textContent = verified ? "Audit chain verified." : "Audit or turn verification failed.";
  recovered = s.read_only || !verified;
  $("recovery").hidden = !recovered;
  $("recovery").textContent = !verified ? "Audit or turn verification failed. Start a new chat; this session remains blocked."
    : "Recovered session: history is available, but new turns are locked. Start a new chat to continue.";
  controls();
}
async function afterConnect() {
    $("write-policy").textContent = caps.governed_writes_enabled
      ? "Development governed writes enabled by operator. New local memories still start as drafts."
      : "Governed writes disabled · new local memories stay draft. Production EMR gates are not enabled.";
    if (caps.recall_configured && caps.recall_owner_user_id) $("user-id").value = caps.recall_owner_user_id;
    $("recall-state").textContent = caps.recall_configured
      ? (authMode === "oauth"
        ? "Account-scoped recall ready. Only verified prior history for this sign-in will be used."
        : "Operator-scoped recall ready. Only verified prior history will be used.")
      : "Cross-session recall is not configured on this server.";
    connected = true;
    $("connection").textContent = caps.chat_configured ? caps.provider + " chat configured" : "Local responder · NVIDIA not configured";
    $("provider").textContent = caps.provider + " / " + caps.model;
    if (!caps.speech_configured && $("mode").value === "voice") textMode("Voice is not configured.");
    const saved = storage.read();
    if (!session && saved?.session && saved.user === $("user-id").value) {
      const data = await post("/sessions/resume", { session_id: saved.session, user_id: saved.user });
      setSession(saved.session); $("messages").replaceChildren();
      for (const m of data.memory.conversation_history) message(m.role === "user" ? "user" : "jarvis", m.content, null, m.turn_id);
    }
    if (session) await governance();
    activity(recovered ? "Start a new chat to continue." : "Ready.");
}
$("connect-form").onsubmit = async e => {
  e.preventDefault(); if (busy || authMode === "oauth") return;
  busy = true; controls(); error(); activity("Connecting…");
  try {
    caps = await request("/capabilities");
    await afterConnect();
  } catch (e) { error(e.message); activity("Connection needs attention."); }
  finally { busy = false; controls(); }
};
$("new-chat").onclick = () => {
  governanceGeneration++; clearInspection();
  stopAudio(); session = ""; recovered = false; storage.clear();
  $("messages").replaceChildren(); $("session-label").textContent = "New conversation";
  $("recovery").hidden = true; error(); $("message").value = ""; $("consent").checked = false;
  for (const id of ["state", "trace", "audit"]) $(id).textContent = "No session yet.";
  $("decision").textContent = "No response yet."; $("confidence").textContent = "—"; $("uncertainty").textContent = "—";
  $("latency").textContent = "—"; $("cost").textContent = "No response yet.";
  $("memory-state").textContent = "No session loaded."; $("audit-state").textContent = "Audit not checked.";
  $("recall-state").textContent = "Recall will be checked when you send a message.";
  controls(); activity(connected ? "Ready." : "Connect to start."); $("message").focus();
};
async function send(inputMode = "text") {
  if (busy || !connected || recovered || recording) return;
  const text = $("message").value.trim(); if (!text) return;
  busy = true; controls(); error(); stopAudio(); activity("Jarvis is thinking…");
  try {
    const d = await post("/chat", { user_id: $("user-id").value.trim(), session_id: session || null,
      message: text, input_mode: inputMode, memory_consent: $("consent").checked,
      recall_previous: !!caps.recall_configured && $("recall").checked });
    setSession(d.session_id); message("user", text); message("jarvis", d.reply, d); decision(d); $("message").value = "";
    try { await governance(); } catch (e) { error("Reply received, but governance refresh failed: " + e.message); }
    activity("Reply received.");
    if (d.safe_mode) { textMode("Inference " + d.inference_status + ". Jarvis is in basic safe-response mode."); return; }
    // Speech is optional: typing must not wait for the speech service.
    if ($("spoken").checked) speak(d.turn_id);
  } catch (e) { error(e.message + " Your message is still in the composer."); activity("Request failed."); }
  finally { busy = false; controls(); $("message").focus(); }
}
$("composer").onsubmit = e => { e.preventDefault(); send("text"); };
$("message").onkeydown = e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send("text"); }
};
$("mode").onchange = () => {
  const voice = $("mode").value === "voice"; $("mic").hidden = !voice; $("voice-note").hidden = !voice;
  if (voice && connected && !caps.speech_configured) { textMode("Voice is not configured."); return; }
  $("spoken").checked = voice; if (!voice) stopAudio();
};
$("spoken").onchange = () => { if (!$("spoken").checked) stopAudio(); };
$("stop-audio").onclick = () => { stopAudio(); activity("Audio stopped."); };
$("refresh").onclick = () => governance().catch(e => error(e.message));
$("sign-out").onclick = async () => {
  try {
    await fetch("/auth/logout", {
      method: "POST",
      headers: csrf ? { "X-Jarvis-CSRF": csrf } : {},
      credentials: "same-origin",
      cache: "no-store"
    });
  } finally { location.href = "/ui/"; }
};
async function bootstrapAuth() {
  const params = new URLSearchParams(location.search);
  if (params.get("login") === "failed") error("Google sign-in did not complete. Try again.");
  try {
    const identity = await fetch("/auth/session", { cache: "no-store", credentials: "same-origin" }).then(r => r.json());
    authMode = identity.auth_mode || "operator";
    csrf = identity.csrf || "";
    if (authMode === "oauth") {
      $("user-field").hidden = true; $("token-field").hidden = true; $("connect").hidden = true;
      $("operator-hint").hidden = true; $("oauth-hint").hidden = false;
      $("google-login").hidden = identity.authenticated;
      $("sign-out").hidden = !identity.authenticated;
      $("token").required = false; $("user-id").required = false;
      if (!identity.authenticated) {
        $("connection").textContent = "Sign in with Google to chat";
        activity("Sign in to start.");
        controls();
        return;
      }
      $("user-id").value = identity.user_id;
      busy = true; controls(); activity("Connecting…");
      try {
        caps = await request("/capabilities");
        await afterConnect();
      } catch (e) { error(e.message); activity("Connection needs attention."); }
      finally { busy = false; controls(); }
    }
  } catch {
    $("connection").textContent = "Service unavailable";
  }
}
async function finishRecording() {
  if (!recording) return;
  const capture = recording; recording = null; clearTimeout(timer);
  busy = true; controls(); $("mic").textContent = "Start recording"; activity("Transcribing…");
  try {
    const wav = await capture.stop();
    const d = await request("/voice/transcribe", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    $("message").value = d.text; busy = false; controls(); await send("voice");
  } catch (e) { textMode("Transcription: " + e.message); }
  finally { busy = false; controls(); }
}
$("mic").onclick = async () => {
  if (recording) { await finishRecording(); return; }
  if (busy || recordingStart) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) { textMode("Microphone access is unavailable in this browser."); return; }
  recordingStart = true; controls(); error(); stopAudio();
  try {
    recording = await createRecorder(); $("mic").textContent = "Stop & send";
    activity("● Recording · stop when finished");
    timer = setTimeout(finishRecording, 29500);
  } catch (e) { textMode("Microphone could not start: " + e.message); }
  finally { recordingStart = false; controls(); }
};
window.addEventListener("pagehide", () => { clearTimeout(timer); recording?.stop(); stopAudio(); });
fetch("/health", { cache: "no-store" }).then(r => { $("connection").textContent = r.ok ? "Service reachable · connect to chat" : "Service unavailable"; })
  .catch(() => { $("connection").textContent = "Service unavailable"; });
bootstrapAuth();
controls();
