"""DOS-lite turn deliberation: Observe → Infer → Challenge → Simulate → Commit.

Internal stages only. Do not emit hidden chain-of-thought markup.
External tools and Infinity results are evidence, never authority.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

ClaimTag = Literal["observed", "specified", "hypothesized"]


class EvidenceKind(str, Enum):
    MEMORY = "memory"
    HISTORY = "history"
    TOOL_EXTERNAL = "tool_external"
    HYPOTHESIZED_NONE = "hypothesized_none"

HEDGE = re.compile(
    r"\b(maybe|might|perhaps|possibly|i don't know|i do not know|not sure|"
    r"uncertain|pausing|unverified|hypothesized|i cannot)\b",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
UNKNOWN = re.compile(
    r"\b(i don't know|i do not know|i'm pausing|i am pausing|not available in this context)\b",
    re.IGNORECASE,
)


def _clip(text: str, limit: int = 280) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in SENTENCE.split(text.strip()) if part.strip()]
    return [part for part in parts if len(part) > 12]


def claim(text: str, tag: ClaimTag, *, source: str, authority: bool = False) -> dict[str, Any]:
    return {
        "text": _clip(text),
        "tag": tag,
        "source": source,
        "authority": False if tag == "hypothesized" else authority,
    }


def admit_external(source: str, payload: Any, *, observed: bool = False) -> dict[str, Any]:
    """Admit a tool, RAG, or Infinity result as evidence. Never as authority."""
    text = payload if isinstance(payload, str) else str(payload)[:500]
    tag: ClaimTag = "observed" if observed and source else "hypothesized"
    item = claim(text or "(empty external result)", tag, source=source, authority=False)
    item["kind"] = EvidenceKind.TOOL_EXTERNAL.value
    return item


def admit_tool(record: dict[str, Any] | Any) -> dict[str, Any]:
    """Admit a ToolCallRecord as TOOL_EXTERNAL evidence. Never as authority, never as a write."""
    payload = record.public_dict() if hasattr(record, "public_dict") else dict(record)
    citations = payload.get("citations") or []
    locators = [str(item.get("locator") or "") for item in citations if isinstance(item, dict)]
    summary = ", ".join(locators[:4]) or str(payload.get("error") or payload.get("status") or "no citations")
    observed = bool(payload.get("observed") and citations)
    item = admit_external(str(payload.get("tool") or "tool"), summary, observed=observed)
    item["args_hash"] = str(payload.get("args_hash") or "")
    item["payload_hash"] = str(payload.get("payload_hash") or "")
    item["status"] = str(payload.get("status") or "")
    item["writes"] = False
    return item


def is_unknown_reply(reply: str) -> bool:
    return bool(UNKNOWN.search(reply or ""))


def infer_claims(
    reply: str,
    *,
    user_message: str,
    citations: list[dict[str, Any]] | None = None,
    external: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    if (user_message or "").strip():
        claims.append(claim(user_message, "specified", source="user"))
    grounded = (user_message or "").lower()
    for citation in citations or []:
        excerpt = str(citation.get("excerpt") or citation.get("preview") or "")
        if excerpt:
            claims.append(
                claim(
                    excerpt,
                    "observed",
                    source=str(citation.get("source_type") or citation.get("memory_id") or "citation"),
                )
            )
            grounded += " " + excerpt.lower()
    for item in external or []:
        admitted = dict(item)
        admitted["authority"] = False
        claims.append(admitted)
        grounded += " " + str(admitted.get("text") or "").lower()
    for sentence in _sentences(reply):
        if UNKNOWN.search(sentence):
            continue
        overlap = any(token and token in grounded for token in sentence.lower().split() if len(token) > 5)
        claims.append(
            claim(
                sentence,
                "observed" if overlap else "hypothesized",
                source="reply" if overlap else "model",
            )
        )
    return claims


def challenge_claims(claims: list[dict[str, Any]]) -> str | None:
    unsupported = [
        item
        for item in claims
        if item.get("tag") == "hypothesized"
        and item.get("source") == "model"
        and not HEDGE.search(str(item.get("text") or ""))
    ]
    if not unsupported:
        return None
    return (
        f"{len(unsupported)} hypothesized claim(s) lack a cited source. "
        "Treat them as unverified, not as established fact."
    )


def deliberate(
    reply: str,
    *,
    user_message: str,
    fail_closed: bool,
    citations: list[dict[str, Any]] | None = None,
    external: list[dict[str, Any]] | None = None,
    tool_records: list[Any] | None = None,
) -> dict[str, Any]:
    """Run Observe → Infer → Challenge → Simulate(no-op) → Commit.

    Commit needs Challenge plus evidence or an unknown/fail-closed reply.
    """
    records = []
    for item in tool_records or []:
        records.append(item.public_dict() if hasattr(item, "public_dict") else dict(item))
    admitted = list(external or [])
    if not admitted:
        admitted = [admit_tool(item) for item in records]
    stages = [
        {"name": "observe", "status": "completed" if records else "skipped"},
        {"name": "infer", "status": "started"},
        {"name": "challenge", "status": "blocked"},
        {"name": "simulate", "status": "skipped"},
        {"name": "commit", "status": "blocked"},
    ]
    claims = infer_claims(reply, user_message=user_message, citations=citations, external=admitted)
    stages[1]["status"] = "completed"
    warning = challenge_claims(claims)
    stages[2]["status"] = "completed"
    stages[3]["status"] = "skipped"
    unknown = is_unknown_reply(reply)
    challenge_done = stages[2]["status"] == "completed"
    has_evidence = any(item.get("tag") in {"observed", "specified"} for item in claims)
    envelope = {
        "stages": stages,
        "claims": claims,
        "committed": False,
        "unsupported_claim_warning": warning,
        "tool_records": records,
        "observe_required": bool(records),
    }
    may_commit = challenge_done and (fail_closed or unknown or has_evidence)
    if not may_commit:
        return envelope
    stages[4]["status"] = "completed"
    envelope["committed"] = True
    return envelope


def withheld_commit_reply() -> str:
    return (
        "I don't know enough from verified evidence to commit that answer. "
        "I can clarify the question or continue with read-only planning."
    )
