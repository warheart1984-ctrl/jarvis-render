const DRAFT_KEY = "jarvis.composer_draft";

export function readDraft() {
  try { return localStorage.getItem(DRAFT_KEY) || ""; } catch { return ""; }
}

export function writeDraft(text) {
  try {
    const value = String(text || "");
    if (value) localStorage.setItem(DRAFT_KEY, value);
    else localStorage.removeItem(DRAFT_KEY);
  } catch { /* private mode or quota */ }
}

export function preserveDraftOnNewChat(currentText) {
  const typed = String(currentText || "");
  if (typed) writeDraft(typed);
  return readDraft();
}
