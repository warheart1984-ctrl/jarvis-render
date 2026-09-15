// No HTML interpolation or external artifact navigation: stored content is untrusted.
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function fields(values) {
  const list = element("dl", undefined, "provenance-fields");
  for (const [label, value] of values) {
    list.append(element("dt", label), element("dd", value ?? "Not recorded"));
  }
  return list;
}
export function receiptSummary(receipt) {
  const status = receipt?.status;
  if (status === "included") return `${receipt.citations?.length || 0} context sources`;
  return ({not_recorded: "Not recorded for this turn", no_inference: "No accepted inference",
    not_requested: "No model request", unavailable: "Verification unavailable"})[status] || "Not recorded for this turn";
}
export function receiptView(receipt, turnId = "") {
  const detail = element("details", undefined, "context-receipt");
  detail.dataset.turnId = turnId;
  detail.append(element("summary", "Influenced this turn · " + receiptSummary(receipt)));
  detail.append(element("p", "These are sources included in the accepted model request—not proof of causal influence or factual truth. The current message is not a recalled source.", "hint"));
  if (turnId) detail.append(fields([["Turn", turnId]]));
  const sources = receipt?.status === "included" ? receipt.citations || [] : [];
  if (!sources.length) {
    detail.append(element("p", receipt?.status === "included"
      ? "No saved memory or earlier history was included."
      : "No source inclusion is confirmed by this receipt.", "hint"));
  }
  const list = element("ol", undefined, "citation-list");
  for (const source of sources) {
    const item = element("li");
    const entry = element("details");
    entry.append(element("summary", source.source_type === "memory"
      ? "Memory · " + source.memory_id : "Conversation history · " + (source.role || "message")));
    entry.append(fields([
      ["Citation", source.citation_id], ["Memory ID", source.memory_id ?? "Not a memory record"],
      ["Source session", source.session_id], ["Content SHA-256 (full text)", source.content_sha256],
      ["Excerpt SHA-256 (model input)", source.excerpt_sha256],
      ["Excerpt", `${source.excerpt_chars} characters${source.truncated ? " · truncated" : " · complete"}`],
      ["Checkpoint", source.checkpoint_id ?? "Current session"],
      ["Ledger ID", source.ledger_memory_id ?? "Not linked"],
      ["AMUL artifact", source.amul_artifact ? JSON.stringify(source.amul_artifact) : "Not linked"],
    ]));
    if (source.amul_artifact) entry.append(element("p", "Artifact reference only; artifact contents have not been verified.", "hint"));
    item.append(entry); list.append(item);
  }
  detail.append(list);
  return detail;
}
export function deliberationSummary(deliberation) {
  const stages = deliberation?.stages || [];
  if (!stages.length) return "not run";
  return stages.map(stage => stage.name).join(" → ");
}
export function coverageLabel(coverage) {
  if (!coverage || coverage.sentence_count == null) return "not recorded";
  const tagged = coverage.tagged_count ?? 0;
  const total = coverage.sentence_count;
  const matcher = coverage.matcher || "v0-token-overlap";
  if (coverage.complete) return `${tagged}/${total} sentences tagged · ${matcher}`;
  return `${tagged}/${total} sentences tagged · incomplete · ${matcher}`;
}
export function deliberationView(deliberation) {
  const detail = element("details", undefined, "deliberation-trace");
  detail.append(element("summary", "DOS-lite v0 · " + deliberationSummary(deliberation)));
  detail.append(element("p", deliberation?.label
    || "v0 heuristic deliberation pipeline (DOS-lite); not a full DOS Kernel, trained judge, or private chain-of-thought engine", "hint"));
  if (deliberation?.challenge_action) {
    detail.append(fields([
            ["Challenge", deliberation.challenge_action],
      ["Response commit", deliberation.response_commit || (deliberation.committed ? "committed" : "refused")],
      ["Memory admission", deliberation.memory_admission || "not recorded"],
      ["Committed", deliberation.committed ? "yes" : "no"],
      ["Status", deliberation.status || "not_run"],
      ["Reply coverage", coverageLabel(deliberation.reply_coverage)],
    ]));
  }
  const claims = deliberation?.claims || [];
  if (claims.length) {
    const list = element("ol", undefined, "citation-list claim-list");
    for (const claim of claims) {
      const item = element("li");
      item.append(element("p", `${(claim.tag || "hypothesized").toUpperCase()} · ${(claim.claim_class || "interpretive")} · ${claim.text || ""}`));
      if (claim.support || claim.action) {
        item.append(element("p", `support=${claim.support || "missing"} · severity=${claim.severity || "harmless"} · action=${claim.action || "continue"}`, "hint"));
      }
      if (claim.unsupported) item.append(element("p", "Unsupported: no evidence reference for this claim.", "hint"));
      list.append(item);
    }
    detail.append(list);
  }
  for (const warning of deliberation?.unsupported_claim_warnings || []) {
    detail.append(element("p", warning, "hint"));
  }
  for (const sentence of deliberation?.reply_coverage?.uncovered || []) {
    detail.append(element("p", "Uncovered reply sentence: " + sentence, "hint"));
  }
  return detail;
}
export function cerView(cer) {
  const detail = element("details", undefined, "cer-record");
  detail.append(element("summary", "CER · replayable turn record"));
  detail.append(element("p", "CER is stored on the existing audit. Not a separate ledger.", "hint"));
  if (!cer || cer.status === "not_recorded") {
    detail.append(element("p", "Not recorded for this turn.", "hint"));
    return detail;
  }
  const identity = cer.model_identity || {};
  const replay = cer.replay || {};
  const lineage = cer.lineage || {};
  const verification = cer.verification || {};
  detail.append(fields([
    ["CER version", cer.version],
    ["Provider / model", (identity.provider || "Not recorded") + " / " + (identity.model || "Not recorded")],
    ["Challenge", verification.challenge],
    ["Previous turn", lineage.previous_turn_id ?? "None"],
    ["Input SHA-256", replay.input_sha256],
    ["Content SHA-256", replay.content_sha256],
  ]));
  return detail;
}
export function renderInspection(root, data) {
  root.replaceChildren();
  if (!data || data.status !== "available") {
    root.append(element("p", data?.status === "unverified"
      ? "Verification failed. Memory records and receipts are withheld."
      : data?.status === "withheld" ? "This session's memory has been cleared. Inspection is withheld."
      : "No verified inspection loaded. Connect and send a message, then refresh governance.", "hint"));
    return;
  }
  root.append(element("p", `${data.records.length} inspectable records · ${data.withheld_records} withheld. History citations are listed separately from extracted memories.`, "hint"));
  if (data.previous_session && !["verified", "not_used"].includes(data.previous_session.status)) {
    root.append(element("p", "Prior source verification: " + data.previous_session.status + ". Prior record contents are withheld; historical receipts describe inclusion at the time of the turn.", "hint"));
  }
  const grid = element("div", undefined, "memory-grid");
  const memories = element("section");
  memories.append(element("h3", "Remembered items"));
  if (!data.records.length) memories.append(element("p", "No extracted memory records available in this view. Conversation recall can still appear in turn citations.", "hint"));
  for (const record of data.records) {
    const card = element("details", undefined, "memory-record");
    card.append(element("summary", `${statusLabel(record)} · ${record.preview.slice(0, 90)}`));
    card.append(element("p", record.preview, "memory-preview"));
    if (record.preview_truncated) card.append(element("p", "Preview truncated. Hash identifies the full stored text.", "hint"));
    card.append(fields([
      ["Memory ID", record.memory_id], ["Content SHA-256", record.content_sha256],
      ["Source session", record.session_id], ["Created", record.created_at],
      ["Integrity", record.integrity === "audit_bound" ? "Hash and metadata match the audit record" : "Hash matches storage; no creation receipt recorded"],
      ["Ledger ID", record.ledger_memory_id ?? "Not linked"],
      ["Reconciled", record.reconciled ? "yes" : "no"],
      ["Supersedes", record.supersedes ?? "None"],
      ["Superseded by", record.superseded_by ?? "None"],
      ["AMUL artifact", record.amul_artifact ? JSON.stringify(record.amul_artifact) : "Not linked"],
    ]));
    card.append(element("p", "Integrity is not truth or approval. Linked artifact contents are not checked by this view.", "hint"));
    if (data.governed_writes_enabled && record.ledger_memory_id && record.status !== "superseded") {
      card.append(supersedeForm(record));
    }
    memories.append(card);
  }
  if (data.conflicts?.length) {
    const conflicts = element("section", undefined, "conflict-panel");
    conflicts.append(element("h3", "Conflict membrane"));
    conflicts.append(element("p", data.lock_reason === "conflict" || data.session_read_only
      ? "This session is read-only until an operator supersedes a conflicting claim or you start a new chat."
      : "Conflict events were recorded for this session.", "hint"));
    for (const item of data.conflicts) {
      const block = element("details", undefined, "memory-record");
      block.append(element("summary", `Conflict · ${item.memory_id || "unknown memory"}`));
      block.append(fields([
        ["Memory ID", item.memory_id],
        ["Reason", item.reason ?? "conflict-membrane"],
        ["Mode", item.mode ?? "read_only"],
        ["Timestamp", item.timestamp],
        ["Conflicting claims", item.conflicts?.length ? JSON.stringify(item.conflicts) : "None listed"],
      ]));
      const target = data.records.find(r => r.memory_id === item.memory_id);
      if (data.governed_writes_enabled && target?.ledger_memory_id && target.status !== "superseded") {
        block.append(supersedeForm(target));
      }
      conflicts.append(block);
    }
    grid.append(conflicts);
  }
  const turns = element("section");
  turns.append(element("h3", "Turn citations"), element("p", "Latest 20 turns. Receipts describe context supplied at that time, not current source availability. Older turns without receipts are marked not recorded.", "hint"));
  for (const turn of [...data.turns].reverse()) {
    turns.append(receiptView(turn.context_receipt, turn.turn_id));
    if (turn.deliberation) turns.append(deliberationView(turn.deliberation));
    if (turn.cer) turns.append(cerView(turn.cer));
  }
  grid.append(memories, turns); root.append(grid);
}
function statusLabel(record) {
  if (record.status === "superseded") return "SUPERSEDED";
  if (record.status === "draft") return "DRAFT";
  if (record.status === "active") return "ACTIVE";
  if (record.status === "archived") return "ARCHIVED";
  return "LEGACY · unreviewed";
}
function supersedeForm(record) {
  const form = element("form", undefined, "supersede-form");
  form.append(element("p", "Operator supersession replaces this ledger-linked claim. Requires service token and user_requested consent.", "hint"));
  const label = element("label", "Replacement content");
  const area = document.createElement("textarea");
  area.name = "content";
  area.rows = 3;
  area.maxLength = 4000;
  area.required = true;
  area.placeholder = "Enter the corrected claim…";
  label.append(area);
  form.append(label);
  const consent = element("label", undefined, "consent");
  const check = document.createElement("input");
  check.type = "checkbox";
  check.name = "user_requested";
  check.required = true;
  consent.append(check);
  consent.append(element("span", " I explicitly request this supersession"));
  form.append(consent);
  const button = document.createElement("button");
  button.type = "submit";
  button.className = "secondary";
  button.textContent = "Supersede claim";
  form.append(button);
  const status = element("p", "", "hint");
  form.append(status);
  if (typeof form.addEventListener === "function") {
    form.addEventListener("submit", event => {
      event.preventDefault();
      const detail = {
        memory_id: record.memory_id,
        content: area.value.trim(),
        user_requested: check.checked,
      };
      if (!detail.content || !detail.user_requested) {
        status.textContent = "Replacement text and explicit consent are required.";
        return;
      }
      status.textContent = "Submitting supersession…";
      form.dispatchEvent(new CustomEvent("jarvis:supersede", { bubbles: true, detail }));
    });
  }
  return form;
}
