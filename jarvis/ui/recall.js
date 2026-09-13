export function recallStatus(info) {
  if (info?.status === "verified") {
    return "Read-only recall: " + info.source_session_id + " · " + (info.history_messages ?? 0)
      + " history messages in a bounded excerpt. "
      + (info.attestation === "operator_attested_legacy" ? "Legacy history attested by the operator." : "Signed checkpoint verified.");
  }
  const labels = {
    disabled: "Previous-session recall is off for this turn.",
    not_authorized: "Recall is not configured for this authenticated operator.",
    no_eligible_history: "No eligible signed prior conversation is available.",
    unverified: "Prior history could not be verified and was not sent to the model.",
    unavailable: "Recall is unavailable. No prior history was sent to the model.",
    withheld: "Prior conversation was cleared and is excluded from recall."
  };
  return labels[info?.status] || "Recall has not been checked yet.";
}
