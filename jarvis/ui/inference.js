export function fallbackWaitMessage(slots) {
  const list = (slots || [])
    .filter(slot => slot && (slot.provider || slot.model))
    .map(slot => `${slot.provider || "provider"} / ${slot.model || "model"}`);
  if (!list.length) {
    return "Waiting for a reply. Inference is not yet confirmed.";
  }
  const primary = list[0];
  const rest = list.slice(1);
  if (!rest.length) {
    return `Trying ${primary}. Inference is not yet confirmed.`;
  }
  return `Trying ${primary}; fallback ${rest.join(" → ")}. Inference is not yet confirmed.`;
}
