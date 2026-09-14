const LEGACY_DRAFT_KEY = "jarvis.composer_draft";
const DRAFT_PREFIX = "jarvis.composer_draft.";

function normalizeUserId(userId) {
  return String(userId || "").trim();
}

export function draftStorageKey(userId) {
  const id = normalizeUserId(userId);
  return id ? DRAFT_PREFIX + id : "";
}

export function readDraft(userId) {
  const key = draftStorageKey(userId);
  if (!key) return "";
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}

export function writeDraft(userId, text) {
  const key = draftStorageKey(userId);
  if (!key) return;
  try {
    const value = String(text || "");
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch { /* private mode or quota */ }
}

export function composerDraftAfterAuth({ userId, authenticated = false } = {}) {
  if (!authenticated) return "";
  return readDraft(userId);
}

export function clearDraftsOnLogout(userId) {
  try {
    localStorage.removeItem(LEGACY_DRAFT_KEY);
    const key = draftStorageKey(userId);
    if (key) localStorage.removeItem(key);
  } catch { /* private mode */ }
}

export function preserveDraftOnNewChat(userId, currentText) {
  const typed = String(currentText || "");
  if (typed) writeDraft(userId, typed);
  return readDraft(userId);
}
