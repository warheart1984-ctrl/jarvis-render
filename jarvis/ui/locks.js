export const LOCK_REASONS = {
  recovery: "Recovered session: history is available, but new turns are locked (reason=recovery). Start a new chat to continue.",
  conflict: "Session is locked pending conflict resolution (reason=conflict). Start a new chat to continue.",
  verification: "Audit or turn verification failed (reason=verification). Start a new chat; this session remains blocked."
};

export function lockBanner({ lockReason, verified = true, readOnly = false } = {}) {
  if (verified === false) {
    return { reason: "verification", text: LOCK_REASONS.verification };
  }
  if (!readOnly) return null;
  const reason = LOCK_REASONS[lockReason] ? lockReason : "recovery";
  return { reason, text: LOCK_REASONS[reason] };
}
