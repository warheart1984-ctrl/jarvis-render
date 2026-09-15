"""v0 / DOS-lite heuristic deliberation pipeline.

Ports the useful core of a DOS Kernel-style turn (Observe → Interpret → Infer →
Challenge or Simulate → Evaluate → Commit) as application heuristics. This is
**not** a full constitutional OS, trained judge, or private chain-of-thought
engine. Same honesty bar as the v0 emotion classifier and spiral-state tracker.

Hard rules:

1. No Commit without at least one evidence reference (memory citation, history
   citation, tool/external suggestion marked as evidence, or explicit
   ``none — hypothesized``).
2. Infer must be followed by Challenge or Simulate before Commit unless a
   waiver is explicitly recorded.
3. External / tool / RAG / other-agent text enters as evidence, never as
   authority (Voss external-suggestion admission).
4. Final answers carry CRS-style claim tags (Observed / Specified /
   Hypothesized) plus unsupported-claim warnings when a claim lacks evidence.
5. Claims also carry a v0 **class** (conversational / interpretive / factual /
   causal / safety-critical). Challenge uses that class so ``require_evidence``
   can qualify, downgrade, revise, or block. Conversational gaps stay optional.
   ``require_evidence`` plus an unsupported factual/safety claim is not a silent
   committed answer. The current user utterance is context, not automatic
   support for factual/causal/safety-critical claims. Abstain is not
   ``committed=true``. Response commit and memory admission are separate gates.

Public traces omit secrets, hidden prompts, and private reasoning.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.governance.fail_closed import (
    is_stress_fail_closed,
    is_uncertainty_fail_closed,
)
from jarvis.governance.hashing import content_hash
from jarvis.governance.secrets import is_secret_key, omit_secrets

DOS_LITE_VERSION = "v0-dos-lite"
DOS_LITE_LABEL = (
    "v0 heuristic deliberation pipeline (DOS-lite); not a full DOS Kernel, "
    "trained judge, or private chain-of-thought engine"
)
_SUMMARY_LIMIT = 240
_CLAIM_LIMIT = 16
_HYPOTHETICAL_HINTS = ("what if", "suppose", "imagine", "hypothetically")
_RESTATEMENT_GLUE = (
    "you said",
    "your request",
    "you asked",
    "you wanted",
    "as you requested",
    "as requested",
)
_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|none|neither|nor|without|cannot|can'?t|don'?t|doesn'?t|"
    r"didn'?t|isn'?t|aren'?t|wasn'?t|weren'?t|won'?t|wouldn'?t|shouldn'?t|"
    r"couldn'?t|false|deny|denies|denied)\b",
    re.IGNORECASE,
)
_CONTENT_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "about",
        "also",
        "and",
        "are",
        "been",
        "being",
        "but",
        "can",
        "did",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "here",
        "into",
        "just",
        "not",
        "our",
        "out",
        "over",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "was",
        "were",
        "what",
        "when",
        "which",
        "will",
        "with",
        "you",
        "your",
    }
)
_CLARIFY_HINTS = ("maybe", "not sure", "which one", "or should", "what do you think")
_FACTUAL_HINTS = ("what is", "who is", "when did", "prove", "is it true", "fact check")


class DeliberationBlocked(ValueError):
    """Raised when a v0 DOS-lite hard rule refuses Commit."""


class DeliberationStageName(str, Enum):
    OBSERVE = "observe"
    INTERPRET = "interpret"
    INFER = "infer"
    CHALLENGE = "challenge"
    SIMULATE = "simulate"
    EVALUATE = "evaluate"
    COMMIT = "commit"


class EvidenceKind(str, Enum):
    MEMORY = "memory"
    HISTORY = "history"
    TOOL_EXTERNAL = "tool_external"
    HYPOTHESIZED_NONE = "hypothesized_none"


class ClaimTag(str, Enum):
    OBSERVED = "observed"
    SPECIFIED = "specified"
    HYPOTHESIZED = "hypothesized"


class ChallengeAction(str, Enum):
    CONTINUE = "continue"
    CLARIFY = "clarify"
    ABSTAIN = "abstain"
    FAIL_CLOSED = "fail_closed"
    REQUIRE_EVIDENCE = "require_evidence"
    QUALIFY = "qualify"
    DOWNGRADE = "downgrade"
    REVISE = "revise"
    BLOCK = "block"


class ClaimClass(str, Enum):
    CONVERSATIONAL = "conversational"
    INTERPRETIVE = "interpretive"
    FACTUAL = "factual"
    CAUSAL = "causal"
    SAFETY_CRITICAL = "safety_critical"


class ClaimSupport(str, Enum):
    PRESENT = "present"
    WEAK = "weak"
    MISSING = "missing"


class ClaimSeverity(str, Enum):
    HARMLESS = "harmless"
    MEANINGFUL = "meaningful"
    BLOCKING = "blocking"


_ACTION_RANK = {
    ChallengeAction.CONTINUE: 0,
    ChallengeAction.DOWNGRADE: 1,
    ChallengeAction.REQUIRE_EVIDENCE: 2,
    ChallengeAction.QUALIFY: 3,
    ChallengeAction.REVISE: 4,
    ChallengeAction.CLARIFY: 5,
    ChallengeAction.ABSTAIN: 6,
    ChallengeAction.BLOCK: 7,
    ChallengeAction.FAIL_CLOSED: 8,
}

_CONVERSATIONAL_HINTS = (
    "good to connect",
    "i'm ready",
    "i am ready",
    "i'm listening",
    "let me know",
    "great question",
    "i can discuss",
    "i can feel",
    "let's go",
    "i'm here",
    "ready when you are",
)
_CAUSAL_HINTS = (
    "this provides",
    "this causes",
    "this ensures",
    "this prevents",
    "therefore",
    "leads to",
    "results in",
    "so that",
)
_SAFETY_CRITICAL_HINTS = (
    "safety boundar",
    "consistent safety",
    "guaranteed safety",
    "fail-closed",
    "fail closed",
    "credential",
    "production deploy",
)
_FACTUAL_ASSERTION_HINTS = (
    " is the ",
    " are the ",
    " is a ",
    " are a ",
    "always ",
    "never ",
    "proven",
    "the capital",
)
QUALIFY_NOTE = (
    "Treat substantive claims without citations as hypothesized. "
    "I do not have enough evidence to establish them as fact."
)
BLOCK_REPLY = (
    "I'm refusing a committed answer on a claim that lacks verification. "
    "I can discuss the goal in read-only terms until there is cited evidence."
)


class EvidenceRef(BaseModel):
    """A public, secret-stripped citation. External items are never authority."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    kind: EvidenceKind
    summary: str = Field(..., max_length=_SUMMARY_LIMIT)
    authority: bool = False
    citation_id: str | None = None
    memory_id: str | None = None
    source: str = ""
    admission: str | None = None
    match_text: str = Field(default="", exclude=True)

    @field_validator("summary")
    @classmethod
    def _bound_summary(cls, value: str) -> str:
        return value[:_SUMMARY_LIMIT]

    @field_validator("authority")
    @classmethod
    def _external_never_authority(cls, value: bool, info: Any) -> bool:
        kind = info.data.get("kind")
        if kind is EvidenceKind.TOOL_EXTERNAL:
            return False
        return value


class ClaimRecord(BaseModel):
    """CRS-style tagged claim plus v0 class, support, and gate action."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    text: str = Field(..., max_length=_SUMMARY_LIMIT)
    tag: ClaimTag
    evidence_ids: list[str] = Field(default_factory=list)
    unsupported: bool = False
    claim_class: ClaimClass = ClaimClass.INTERPRETIVE
    support: ClaimSupport = ClaimSupport.MISSING
    severity: ClaimSeverity = ClaimSeverity.HARMLESS
    action: ChallengeAction = ChallengeAction.CONTINUE


class StageRecord(BaseModel):
    """Public stage label and summary — facts/rules only, no private CoT."""

    model_config = ConfigDict(extra="forbid")

    name: DeliberationStageName
    summary: str = Field(..., max_length=_SUMMARY_LIMIT)


class WaiverRecord(BaseModel):
    """Explicit, recorded skip of Challenge/Simulate after Infer."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=_SUMMARY_LIMIT)
    after_stage: Literal["infer"] = "infer"


class ReplyCoverage(BaseModel):
    """Whether every committed-reply unit was tagged. v0 gate input, not NLI."""

    model_config = ConfigDict(extra="forbid")

    sentence_count: int = 0
    tagged_count: int = 0
    uncovered: list[str] = Field(default_factory=list)
    complete: bool = True
    matcher: str = "v0-token-overlap"


class DeliberationResult(BaseModel):
    """Public DOS-lite envelope for traces, audit, and API metadata."""

    model_config = ConfigDict(extra="forbid")

    version: str = DOS_LITE_VERSION
    label: str = DOS_LITE_LABEL
    status: Literal["committed", "blocked", "in_progress", "qualified", "revised", "refused", "abstained"] = (
        "in_progress"
    )
    stages: list[StageRecord] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    envelopes: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_claim_warnings: list[str] = Field(default_factory=list)
    challenge_action: ChallengeAction | None = None
    challenge_reasons: list[str] = Field(default_factory=list)
    waivers: list[WaiverRecord] = Field(default_factory=list)
    committed: bool = False
    blocked_reason: str | None = None
    response_commit: Literal["committed", "qualified", "revised", "refused", "abstained"] = "committed"
    memory_admission: Literal["eligible", "held", "blocked"] = "eligible"
    reply_coverage: ReplyCoverage = Field(default_factory=ReplyCoverage)

    def to_public_dict(self) -> dict[str, Any]:
        return omit_secrets(self.model_dump(mode="json"))


def empty_deliberation(*, status: str = "not_run") -> dict[str, Any]:
    return omit_secrets(
        {
            "version": DOS_LITE_VERSION,
            "label": DOS_LITE_LABEL,
            "status": status,
            "stages": [],
            "evidence": [],
            "claims": [],
            "unsupported_claim_warnings": [],
            "challenge_action": None,
            "challenge_reasons": [],
            "waivers": [],
            "committed": False,
            "blocked_reason": None,
            "response_commit": "committed",
            "memory_admission": "eligible",
            "reply_coverage": {
                "sentence_count": 0,
                "tagged_count": 0,
                "uncovered": [],
                "complete": True,
                "matcher": "v0-token-overlap",
            },
        }
    )


def _clip(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:_SUMMARY_LIMIT]


def _stage_summary(name: DeliberationStageName, detail: str) -> StageRecord:
    return StageRecord(name=name, summary=_clip(detail))


def admit_external_suggestion(
    *,
    source: str,
    summary: str,
    evidence_id: str | None = None,
    requested_authority: bool = False,
    match_text: str = "",
    citation_id: str | None = None,
) -> EvidenceRef:
    """Admit tool/RAG/other-agent text as evidence only (never authority)."""

    admission = "Voss admission: external/tool/RAG/other-agent text is evidence, never authority"
    if requested_authority:
        admission += "; requested authority flag ignored"
    digest = content_hash({"source": source, "summary": summary})[:16]
    return EvidenceRef(
        evidence_id=evidence_id or f"ext-{digest}",
        kind=EvidenceKind.TOOL_EXTERNAL,
        summary=_clip(summary),
        authority=False,
        source=source,
        admission=admission,
        citation_id=citation_id,
        match_text=_clip(match_text) if match_text else "",
    )


def hypothesized_none(reason: str = "no corroborating citation available") -> EvidenceRef:
    return EvidenceRef(
        evidence_id="none-hypothesized",
        kind=EvidenceKind.HYPOTHESIZED_NONE,
        summary=_clip(f"none — hypothesized ({reason})"),
        authority=False,
        source="explicit-gap",
        admission="Explicit gap marker; not a supporting fact",
    )


def evidence_from_citation(item: dict[str, Any]) -> EvidenceRef | None:
    """Map a provenance citation envelope to DOS-lite evidence (ids/hashes only)."""

    source_type = str(item.get("source_type") or "")
    if source_type == "memory":
        kind = EvidenceKind.MEMORY
    elif source_type == "history":
        kind = EvidenceKind.HISTORY
    else:
        return None
    citation_id = item.get("citation_id")
    memory_id = item.get("memory_id")
    digest = str(citation_id or item.get("content_sha256") or source_type)
    return EvidenceRef(
        evidence_id=f"cite-{digest}"[:80],
        kind=kind,
        summary=_clip(
            f"{source_type} citation"
            + (f" memory_id={memory_id}" if memory_id else "")
            + (f" citation_id={citation_id}" if citation_id else "")
        ),
        authority=False,
        citation_id=citation_id if isinstance(citation_id, str) else None,
        memory_id=memory_id if isinstance(memory_id, str) else None,
        source=source_type,
    )


def message_looks_hypothetical(message: str) -> bool:
    lower = message.lower()
    return any(hint in lower for hint in _HYPOTHETICAL_HINTS)


def choose_challenge_action(
    *,
    message: str,
    uncertainty: float,
    stress: float,
    evidence: list[EvidenceRef],
    confidence: float,
) -> tuple[ChallengeAction, list[str]]:
    """v0 challenge policy aligned with existing fail-closed thresholds."""

    reasons: list[str] = []
    if is_stress_fail_closed(stress):
        reasons.append("stress above safe execution threshold")
        return ChallengeAction.FAIL_CLOSED, reasons
    if is_uncertainty_fail_closed(uncertainty):
        reasons.append("uncertainty at or above safe execution threshold")
        return ChallengeAction.FAIL_CLOSED, reasons

    supported = [
        item
        for item in evidence
        if item.kind is not EvidenceKind.HYPOTHESIZED_NONE and item.evidence_id != "hist-current-utterance"
    ]
    lower = message.lower()
    if not supported and any(hint in lower for hint in _FACTUAL_HINTS):
        reasons.append("factual ask without non-hypothesized evidence")
        return ChallengeAction.REQUIRE_EVIDENCE, reasons
    if confidence < 0.45 or any(hint in lower for hint in _CLARIFY_HINTS):
        reasons.append("ambiguous or low-confidence request; clarification warranted")
        return ChallengeAction.CLARIFY, reasons
    if not supported:
        reasons.append("only hypothesized or missing evidence")
        return ChallengeAction.REQUIRE_EVIDENCE, reasons
    return ChallengeAction.CONTINUE, reasons


def tag_claims(
    *,
    message: str,
    reply: str,
    evidence: list[EvidenceRef],
    emotion_label: str,
    intent: str,
) -> tuple[list[ClaimRecord], list[str], ReplyCoverage]:
    """Attach CRS-style tags. Heuristic only; not model-grade claim extraction."""

    by_id = {item.evidence_id: item for item in evidence}
    history_ids = [item.evidence_id for item in evidence if item.kind is EvidenceKind.HISTORY]
    memory_ids = [item.evidence_id for item in evidence if item.kind is EvidenceKind.MEMORY]
    claims: list[ClaimRecord] = [
        ClaimRecord(
            claim_id="claim-observed-utterance",
            text=_clip("User utterance observed for this turn"),
            tag=ClaimTag.OBSERVED,
            evidence_ids=history_ids[:3],
            unsupported=not history_ids,
        ),
        ClaimRecord(
            claim_id="claim-specified-request",
            text=_clip(f"Specified request: {message}"),
            tag=ClaimTag.SPECIFIED,
            evidence_ids=history_ids[:3],
            unsupported=not history_ids,
        ),
        ClaimRecord(
            claim_id="claim-hypothesized-emotion",
            text=_clip(f"Hypothesized emotion label '{emotion_label}' from the v0 keyword heuristic"),
            tag=ClaimTag.HYPOTHESIZED,
            evidence_ids=[],
            unsupported=True,
        ),
        ClaimRecord(
            claim_id="claim-hypothesized-intent",
            text=_clip(f"Hypothesized session intent '{intent}' from the v0 spiral tracker"),
            tag=ClaimTag.HYPOTHESIZED,
            evidence_ids=[],
            unsupported=True,
        ),
    ]
    if memory_ids:
        claims.append(
            ClaimRecord(
                claim_id="claim-observed-memory",
                text="Session memory citations were available as evidence",
                tag=ClaimTag.OBSERVED,
                evidence_ids=memory_ids[:4],
                unsupported=False,
            )
        )

    reply_units = _coverage_units(reply)
    uncovered: list[str] = []
    tagged_reply = 0
    for index, unit in enumerate(reply_units):
        if len(claims) >= _CLAIM_LIMIT:
            uncovered.append(_clip(unit))
            continue
        claim_id = f"claim-reply-{index}"
        claim_class = classify_claim_class(claim_id=claim_id, text=unit)
        linked = _match_evidence(unit, evidence, utterance=message, claim_class=claim_class)
        tag = ClaimTag.HYPOTHESIZED if not linked else ClaimTag.OBSERVED
        claims.append(
            ClaimRecord(
                claim_id=claim_id,
                text=_clip(unit),
                tag=tag,
                evidence_ids=linked,
                unsupported=not linked,
            )
        )
        tagged_reply += 1

    coverage = ReplyCoverage(
        sentence_count=len(reply_units),
        tagged_count=tagged_reply,
        uncovered=uncovered[:8],
        complete=not uncovered,
        matcher="v0-token-overlap",
    )

    warnings: list[str] = []
    for claim in claims:
        if claim.unsupported or not claim.evidence_ids:
            if claim.tag is ClaimTag.HYPOTHESIZED or claim.unsupported:
                warnings.append(f"unsupported claim ({claim.tag.value}): {claim.text}")
            elif any(eid not in by_id for eid in claim.evidence_ids):
                warnings.append(f"unsupported claim ({claim.tag.value}): {claim.text}")
        if claim.evidence_ids and all(
            by_id.get(eid) is not None and by_id[eid].kind is EvidenceKind.HYPOTHESIZED_NONE
            for eid in claim.evidence_ids
        ):
            claim.unsupported = True
            warnings.append(f"unsupported claim ({claim.tag.value}): {claim.text}")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for warning in warnings:
        if warning not in seen:
            seen.add(warning)
            unique.append(warning)
    if uncovered:
        unique.append(f"whole-answer coverage incomplete: {len(uncovered)} reply sentence(s) not tagged")
    return claims[:_CLAIM_LIMIT], unique, coverage


def classify_claim_class(*, claim_id: str, text: str) -> ClaimClass:
    """v0 keyword class. Not NLI, not a safety certifier."""

    if claim_id in {"claim-hypothesized-emotion", "claim-hypothesized-intent"}:
        return ClaimClass.INTERPRETIVE
    if claim_id in {"claim-observed-utterance", "claim-specified-request", "claim-observed-memory"}:
        return ClaimClass.FACTUAL
    lower = text.lower()
    if any(hint in lower for hint in _SAFETY_CRITICAL_HINTS):
        return ClaimClass.SAFETY_CRITICAL
    if any(hint in lower for hint in _CAUSAL_HINTS):
        return ClaimClass.CAUSAL
    if any(hint in lower for hint in _CONVERSATIONAL_HINTS):
        return ClaimClass.CONVERSATIONAL
    if any(hint in lower for hint in _FACTUAL_HINTS) or any(hint in lower for hint in _FACTUAL_ASSERTION_HINTS):
        return ClaimClass.FACTUAL
    if any(word in lower for word in ("orienting", "intent", "emotion", "i think", "i'm mapping", "spiral")):
        return ClaimClass.INTERPRETIVE
    if len(text) < 48:
        return ClaimClass.CONVERSATIONAL
    return ClaimClass.INTERPRETIVE


def _evidence_justifies(claim: ClaimRecord, item: EvidenceRef) -> bool:
    """Whether an evidence ref can justify (not merely match) this claim. v0, not NLI."""

    if item.kind is EvidenceKind.HYPOTHESIZED_NONE:
        return False
    if not _current_utterance_item(item):
        # Token overlap may cite a candidate; it does not verify a safety-critical assertion.
        return claim.claim_class is not ClaimClass.SAFETY_CRITICAL
    if claim.claim_id in {"claim-observed-utterance", "claim-specified-request"}:
        return True
    match claim.claim_class:
        case ClaimClass.CONVERSATIONAL | ClaimClass.INTERPRETIVE:
            return True
        case ClaimClass.FACTUAL | ClaimClass.CAUSAL | ClaimClass.SAFETY_CRITICAL:
            return False
        case _:
            assert_never(claim.claim_class)


def support_for(claim: ClaimRecord, evidence: list[EvidenceRef]) -> ClaimSupport:
    by_id = {item.evidence_id: item for item in evidence}
    if claim.unsupported or not claim.evidence_ids:
        return ClaimSupport.MISSING
    items = [by_id[eid] for eid in claim.evidence_ids if eid in by_id]
    if not items:
        return ClaimSupport.MISSING
    if all(item.kind is EvidenceKind.HYPOTHESIZED_NONE for item in items):
        return ClaimSupport.MISSING
    justifying = [item for item in items if _evidence_justifies(claim, item)]
    if not justifying:
        return ClaimSupport.MISSING
    if all(item.kind in {EvidenceKind.TOOL_EXTERNAL, EvidenceKind.HYPOTHESIZED_NONE} for item in justifying):
        return ClaimSupport.WEAK
    return ClaimSupport.PRESENT


def action_for_class(claim_class: ClaimClass, support: ClaimSupport) -> ChallengeAction:
    match claim_class:
        case ClaimClass.CONVERSATIONAL:
            return ChallengeAction.CONTINUE
        case ClaimClass.INTERPRETIVE:
            return ChallengeAction.CONTINUE if support is ClaimSupport.PRESENT else ChallengeAction.DOWNGRADE
        case ClaimClass.FACTUAL:
            if support is ClaimSupport.PRESENT:
                return ChallengeAction.CONTINUE
            if support is ClaimSupport.WEAK:
                return ChallengeAction.DOWNGRADE
            return ChallengeAction.BLOCK
        case ClaimClass.CAUSAL:
            if support is ClaimSupport.PRESENT:
                return ChallengeAction.CONTINUE
            if support is ClaimSupport.WEAK:
                return ChallengeAction.REVISE
            return ChallengeAction.BLOCK
        case ClaimClass.SAFETY_CRITICAL:
            return ChallengeAction.CONTINUE if support is ClaimSupport.PRESENT else ChallengeAction.BLOCK
        case _:
            assert_never(claim_class)


def severity_for(claim_class: ClaimClass, support: ClaimSupport) -> ClaimSeverity:
    match claim_class:
        case ClaimClass.CONVERSATIONAL:
            return ClaimSeverity.HARMLESS
        case ClaimClass.INTERPRETIVE:
            return ClaimSeverity.HARMLESS if support is ClaimSupport.PRESENT else ClaimSeverity.MEANINGFUL
        case ClaimClass.FACTUAL | ClaimClass.CAUSAL:
            return ClaimSeverity.HARMLESS if support is ClaimSupport.PRESENT else ClaimSeverity.MEANINGFUL
        case ClaimClass.SAFETY_CRITICAL:
            return ClaimSeverity.HARMLESS if support is ClaimSupport.PRESENT else ClaimSeverity.BLOCKING
        case _:
            assert_never(claim_class)


def annotate_claims(claims: list[ClaimRecord], evidence: list[EvidenceRef]) -> list[ClaimRecord]:
    annotated: list[ClaimRecord] = []
    for claim in claims:
        claim_class = classify_claim_class(claim_id=claim.claim_id, text=claim.text)
        classified = claim.model_copy(update={"claim_class": claim_class})
        support = support_for(classified, evidence)
        annotated.append(
            classified.model_copy(
                update={
                    "support": support,
                    "severity": severity_for(claim_class, support),
                    "action": action_for_class(claim_class, support),
                }
            )
        )
    return annotated


def _stronger_action(left: ChallengeAction | None, right: ChallengeAction) -> ChallengeAction:
    if left is None:
        return right
    return right if _ACTION_RANK[right] > _ACTION_RANK[left] else left


def resolve_claim_gate(
    claims: list[ClaimRecord],
    prior: ChallengeAction | None,
) -> tuple[ChallengeAction, list[str], str, str]:
    """Turn per-claim actions into response-commit and memory-admission gates."""

    reasons: list[str] = []
    resolution = prior or ChallengeAction.CONTINUE
    for claim in claims:
        if not claim.claim_id.startswith("claim-reply"):
            continue
        if claim.claim_class is ClaimClass.CONVERSATIONAL:
            continue
        if claim.claim_class is ClaimClass.INTERPRETIVE:
            continue
        if prior is ChallengeAction.ABSTAIN:
            continue
        resolution = _stronger_action(resolution, claim.action)
        if claim.action is not ChallengeAction.CONTINUE:
            reasons.append(f"{claim.claim_class.value} claim support={claim.support.value} → {claim.action.value}")
    if (
        prior is ChallengeAction.REQUIRE_EVIDENCE
        and _ACTION_RANK[resolution] <= _ACTION_RANK[ChallengeAction.REQUIRE_EVIDENCE]
    ):
        resolution = ChallengeAction.DOWNGRADE
        reasons.append("require_evidence resolved to downgrade: no blocking substantive claim")
    memory: Literal["eligible", "held", "blocked"] = "eligible"
    response: Literal["committed", "qualified", "revised", "refused", "abstained"] = "committed"
    if prior is not ChallengeAction.ABSTAIN and any(
        claim.claim_id.startswith("claim-reply")
        and claim.action is ChallengeAction.BLOCK
        and claim.claim_class in {
            ClaimClass.SAFETY_CRITICAL,
            ClaimClass.FACTUAL,
            ClaimClass.CAUSAL,
        }
        for claim in claims
    ):
        memory = "blocked"
        response = "refused"
        resolution = ChallengeAction.BLOCK
    elif resolution is ChallengeAction.FAIL_CLOSED:
        memory = "blocked"
        response = "refused"
    elif resolution is ChallengeAction.ABSTAIN:
        memory = "held"
        response = "abstained"
    elif any(
        claim.claim_class is ClaimClass.CAUSAL and claim.action is not ChallengeAction.CONTINUE for claim in claims
    ):
        memory = "held"
        response = "revised" if resolution is ChallengeAction.REVISE else response
    elif resolution is ChallengeAction.QUALIFY:
        response = "qualified"
    elif resolution is ChallengeAction.REVISE:
        memory = "held"
        response = "revised"
    return resolution, reasons, response, memory


_TOOL_MEMORY_SOURCES = frozenset(
    {
        "web_search",
        "calculator",
        "clock",
        "weather",
        "document_retrieval",
        "health",
    }
)
_INFERRED_SUMMARY_HINTS = (
    " is the ",
    " are the ",
    " was the ",
    " were the ",
    " according to ",
    " the capital ",
    " equals ",
    " result is ",
)


def apply_write_path_lock(
    claims: list[ClaimRecord],
    evidence: list[EvidenceRef],
    memory_admission: Literal["eligible", "held", "blocked"],
    *,
    reply: str = "",
) -> tuple[Literal["eligible", "held", "blocked"], list[str]]:
    """Refuse hypothesized, tool-cited, and inferred-summary memory writes.

    Durable product lock. ``JARVIS_GOVERNED_WRITES_ENABLED`` cannot reopen this
    path. User-grounded Observed facts may stay eligible when the reply is not
    a hypothesized world-fact or a tool summary; mixed turns fail closed.
    """

    if memory_admission != "eligible":
        return memory_admission, []
    reasons: list[str] = []
    by_id = {item.evidence_id: item for item in evidence}
    if any(item.kind is EvidenceKind.TOOL_EXTERNAL and item.source in _TOOL_MEMORY_SOURCES for item in evidence):
        reasons.append("tool_external evidence cannot be admitted to memory")
    lower_reply = f" {reply.lower()} " if reply else ""
    if lower_reply and any(hint in lower_reply for hint in _INFERRED_SUMMARY_HINTS):
        if any(item.kind is EvidenceKind.TOOL_EXTERNAL for item in evidence) or any(
            claim.claim_id.startswith("claim-reply") and claim.tag is ClaimTag.HYPOTHESIZED for claim in claims
        ):
            reasons.append("inferred summary cannot be admitted to memory")
    for claim in claims:
        if not claim.claim_id.startswith("claim-reply"):
            continue
        kinds = {by_id[eid].kind for eid in claim.evidence_ids if eid in by_id}
        if EvidenceKind.TOOL_EXTERNAL in kinds or EvidenceKind.HYPOTHESIZED_NONE in kinds:
            reasons.append("hypothesized or tool_external claim cannot be admitted to memory")
            continue
        if claim.tag is not ClaimTag.HYPOTHESIZED:
            continue
        # v0 classifies long filler as factual; only hypothesized world-fact /
        # inferred-summary text is refused so user-grounded utterance memory survives.
        text = f" {claim.text.lower()} "
        if any(hint in text for hint in _INFERRED_SUMMARY_HINTS):
            reasons.append("inferred summary cannot be admitted to memory")
    if not reasons:
        return memory_admission, []
    unique: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return "blocked", unique


def apply_coverage_gate(
    coverage: ReplyCoverage,
    resolution: ChallengeAction,
    response_commit: Literal["committed", "qualified", "revised", "refused", "abstained"],
    memory_admission: Literal["eligible", "held", "blocked"],
) -> tuple[
    ChallengeAction,
    list[str],
    Literal["committed", "qualified", "revised", "refused", "abstained"],
    Literal["eligible", "held", "blocked"],
]:
    """Unchecked reply text is a gate, not a dashboard. v0 heuristic, not NLI."""

    if coverage.complete:
        return resolution, [], response_commit, memory_admission
    extra = ["unchecked reply text cannot be a fully committed answer"]
    if response_commit in {"refused", "abstained"}:
        return resolution, extra, response_commit, memory_admission
    if any(
        classify_claim_class(claim_id="claim-uncovered", text=text) is ClaimClass.SAFETY_CRITICAL
        for text in coverage.uncovered
    ):
        return ChallengeAction.BLOCK, extra, "refused", "blocked"
    if response_commit == "committed":
        return _stronger_action(resolution, ChallengeAction.QUALIFY), extra, "qualified", memory_admission
    return resolution, extra, response_commit, memory_admission


def apply_reply_resolution(reply: str, resolution: ChallengeAction, claims: list[ClaimRecord]) -> str:
    match resolution:
        case (
            ChallengeAction.CONTINUE
            | ChallengeAction.DOWNGRADE
            | ChallengeAction.REQUIRE_EVIDENCE
            | ChallengeAction.CLARIFY
            | ChallengeAction.FAIL_CLOSED
        ):
            return reply
        case ChallengeAction.ABSTAIN:
            return challenge_reply(ChallengeAction.ABSTAIN) or reply
        case ChallengeAction.QUALIFY:
            return _clip_reply(reply, QUALIFY_NOTE)
        case ChallengeAction.REVISE:
            hedged = _hedge_substantive(reply, claims)
            return _clip_reply(hedged, QUALIFY_NOTE)
        case ChallengeAction.BLOCK:
            return BLOCK_REPLY
        case _:
            assert_never(resolution)


def _clip_reply(reply: str, note: str) -> str:
    text = reply.strip()
    if note in text:
        return text
    return f"{text} {note}".strip()


def _hedge_substantive(reply: str, claims: list[ClaimRecord]) -> str:
    revised = reply
    for claim in claims:
        if claim.claim_class in {ClaimClass.CAUSAL, ClaimClass.SAFETY_CRITICAL, ClaimClass.FACTUAL} and (
            claim.action in {ChallengeAction.REVISE, ChallengeAction.BLOCK, ChallengeAction.QUALIFY}
        ):
            snippet = claim.text.strip()
            if snippet and snippet in revised and not snippet.lower().startswith("hypothesized"):
                revised = revised.replace(snippet, f"Hypothesized — {snippet}", 1)
    return revised


def _reply_sentences(reply: str) -> list[str]:
    """Split the committed reply so coverage is not limited to keyword-matching clauses."""

    parts = [" ".join(part.split()) for part in re.split(r"[.!?]+", reply or "")]
    return [part for part in parts if part]


def _coverage_units(reply: str) -> list[str]:
    """Sentence-split, then 240-char windows so a long tail cannot skip Challenge."""

    units: list[str] = []
    for sentence in _reply_sentences(reply):
        if len(sentence) <= _SUMMARY_LIMIT:
            units.append(sentence)
            continue
        for start in range(0, len(sentence), _SUMMARY_LIMIT):
            chunk = sentence[start : start + _SUMMARY_LIMIT]
            if chunk:
                units.append(chunk)
    return units


def _content_tokens(text: str, *, stem: bool = True) -> set[str]:
    tokens: set[str] = set()
    for word in _CONTENT_TOKEN_RE.findall((text or "").lower()):
        if len(word) < 4 or word in _STOPWORDS:
            continue
        tokens.add(word)
        if stem and word.endswith("s") and len(word) > 4:
            tokens.add(word[:-1])
    return tokens


def _token_overlap(left: str, right: str) -> int:
    return len(_content_tokens(left) & _content_tokens(right))


def _strip_restatement_glue(text: str) -> str:
    lowered = (text or "").lower()
    for phrase in _RESTATEMENT_GLUE:
        lowered = lowered.replace(phrase, " ")
    return lowered


def _has_negation(text: str) -> bool:
    return bool(_NEGATION_RE.search(text or ""))


def _polarities_conflict(claim: str, evidence: str) -> bool:
    return _has_negation(claim) != _has_negation(evidence)


def _is_primarily_restatement(sentence: str, utterance: str) -> bool:
    """True when most content tokens are already in the user request. Not NLI."""

    body = _strip_restatement_glue(sentence)
    claim_tokens = _content_tokens(body, stem=False)
    utter_tokens = _content_tokens(utterance, stem=False)
    shared = claim_tokens & utter_tokens
    if len(shared) < 2:
        return False
    extra = claim_tokens - utter_tokens
    return len(extra) <= len(shared)


def _utterance_may_justify(claim_class: ClaimClass, sentence: str, utterance: str) -> bool:
    match claim_class:
        case ClaimClass.CONVERSATIONAL | ClaimClass.INTERPRETIVE:
            return True
        case ClaimClass.FACTUAL:
            # Candidate citation of a specified restatement, not justification.
            return _is_primarily_restatement(sentence, utterance)
        case ClaimClass.CAUSAL | ClaimClass.SAFETY_CRITICAL:
            return False
        case _:
            assert_never(claim_class)


def _current_utterance_item(item: EvidenceRef) -> bool:
    return item.evidence_id == "hist-current-utterance" or (
        item.kind is EvidenceKind.HISTORY and item.source == "user_message"
    )


def _match_evidence(
    sentence: str,
    evidence: list[EvidenceRef],
    *,
    utterance: str = "",
    claim_class: ClaimClass = ClaimClass.INTERPRETIVE,
) -> list[str]:
    """v0 linker: content-token overlap with admitted evidence, same polarity.

    Not NLI. Restatement glue ("you asked") is not support. Overlap with a source
    that negates the assertion is not support. Matching the current user request
    does not by itself justify a factual/causal/safety claim.
    """

    body = _strip_restatement_glue(sentence)
    linked: list[str] = []
    for item in evidence:
        if item.kind is EvidenceKind.HYPOTHESIZED_NONE:
            continue
        if item.evidence_id in linked:
            continue
        corpus = item.match_text or item.summary
        if _polarities_conflict(body, corpus):
            continue
        if _current_utterance_item(item) and not _utterance_may_justify(claim_class, sentence, utterance or corpus):
            continue
        token = (item.memory_id or item.source or item.kind.value).lower()
        matched = bool(token and token in body)
        if not matched and _token_overlap(body, corpus) >= 2:
            matched = True
        if (
            not matched
            and _current_utterance_item(item)
            and utterance
            and len(_content_tokens(body) & _content_tokens(utterance)) >= 2
        ):
            matched = True
        if matched:
            linked.append(item.evidence_id)
    return linked[:4]


def challenge_reply(action: ChallengeAction) -> str | None:
    """Optional language-only override when Challenge refuses a committed answer."""

    match action:
        case (
            ChallengeAction.CONTINUE
            | ChallengeAction.REQUIRE_EVIDENCE
            | ChallengeAction.QUALIFY
            | ChallengeAction.DOWNGRADE
            | ChallengeAction.REVISE
        ):
            return None
        case ChallengeAction.CLARIFY:
            return (
                "I need a bit more specificity before I commit to an answer. "
                "Which outcome matters most, and what should I treat as given?"
            )
        case ChallengeAction.ABSTAIN:
            return (
                "I'm abstaining from a committed answer. The available evidence is too thin "
                "or the signals are too uncertain for a DOS-lite v0 commit."
            )
        case ChallengeAction.BLOCK:
            return BLOCK_REPLY
        case ChallengeAction.FAIL_CLOSED:
            return None
        case _:
            assert_never(action)


def map_challenge_decision(action: ChallengeAction, current: str) -> str:
    """Map a challenge action onto the engine decision vocabulary."""

    if current == "fail_closed":
        return current
    match action:
        case (
            ChallengeAction.CONTINUE
            | ChallengeAction.REQUIRE_EVIDENCE
            | ChallengeAction.CLARIFY
            | ChallengeAction.QUALIFY
            | ChallengeAction.DOWNGRADE
            | ChallengeAction.REVISE
        ):
            return current
        case ChallengeAction.ABSTAIN:
            return "abstain"
        case ChallengeAction.BLOCK:
            return "fail_closed"
        case ChallengeAction.FAIL_CLOSED:
            return "fail_closed"
        case _:
            assert_never(action)


class DeliberationRunner:
    """Sequential v0 DOS-lite runner used by the turn engine and unit tests."""

    def __init__(self) -> None:
        self.evidence: list[EvidenceRef] = []
        self.stages: list[StageRecord] = []
        self.claims: list[ClaimRecord] = []
        self.envelopes: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.waivers: list[WaiverRecord] = []
        self.challenge_action: ChallengeAction | None = None
        self.challenge_reasons: list[str] = []
        self.response_commit: Literal["committed", "qualified", "revised", "refused", "abstained"] = "committed"
        self.memory_admission: Literal["eligible", "held", "blocked"] = "eligible"
        self.gated_reply = ""
        self.reply_coverage = ReplyCoverage()
        self._completed: set[DeliberationStageName] = set()
        self._critiqued = False
        self._inputs: dict[str, Any] = {}
        self._reply = ""

    def add_evidence(self, item: EvidenceRef) -> EvidenceRef:
        if item.kind is EvidenceKind.TOOL_EXTERNAL:
            item = admit_external_suggestion(
                source=item.source or "external",
                summary=item.summary,
                evidence_id=item.evidence_id,
                requested_authority=item.authority,
                match_text=item.match_text,
                citation_id=item.citation_id,
            )
        if all(existing.evidence_id != item.evidence_id for existing in self.evidence):
            self.evidence.append(item)
        return item

    def observe(
        self,
        *,
        message: str,
        session_id: str,
        memories: list[tuple[str, str]] | None = None,
        history: list[tuple[str, str]] | None = None,
        external_context: dict[str, Any] | None = None,
        citations: list[dict[str, Any]] | None = None,
        emotion_rationale: list[str] | None = None,
        cite_utterance: bool = True,
    ) -> DeliberationRunner:
        self._inputs.update(message=message, session_id=session_id)
        if cite_utterance:
            self.add_evidence(
                EvidenceRef(
                    evidence_id="hist-current-utterance",
                    kind=EvidenceKind.HISTORY,
                    summary=_clip("Current user utterance (history citation of specified input)"),
                    source="user_message",
                    citation_id=None,
                    match_text=_clip(message),
                )
            )
        for memory_id, summary in memories or []:
            self.add_evidence(
                EvidenceRef(
                    evidence_id=f"mem-{memory_id}",
                    kind=EvidenceKind.MEMORY,
                    summary=_clip(f"memory citation {memory_id}"),
                    memory_id=memory_id,
                    source="memory",
                    match_text=_clip(summary or f"memory {memory_id}"),
                )
            )
        for index, (ref, summary) in enumerate(history or []):
            self.add_evidence(
                EvidenceRef(
                    evidence_id=f"hist-{ref or index}",
                    kind=EvidenceKind.HISTORY,
                    summary=_clip(f"history citation {ref or index}"),
                    source="history",
                    match_text=_clip(summary or f"history {ref}"),
                )
            )
        if external_context:
            keys = [str(key) for key in external_context if not is_secret_key(str(key))]
            self.add_evidence(
                admit_external_suggestion(
                    source="request.context",
                    summary=f"External suggestion envelope with {len(keys)} public keys; values omitted",
                    requested_authority=True,
                )
            )
        for item in citations or []:
            cited = evidence_from_citation(omit_secrets(item) if isinstance(item, dict) else {})
            if cited:
                self.add_evidence(cited)
        rationale = ", ".join(emotion_rationale or []) or "no emotion rationale"
        self._record(
            DeliberationStageName.OBSERVE,
            f"Observed utterance, {len(self.evidence)} evidence refs, emotion cues: {rationale}",
        )
        return self

    def interpret(self, *, emotion_label: str, intent: str, phase: str, confidence: float) -> DeliberationRunner:
        self._inputs.update(emotion_label=emotion_label, intent=intent, phase=phase, confidence=confidence)
        self._record(
            DeliberationStageName.INTERPRET,
            (
                f"Interpreted v0 signals emotion={emotion_label} intent={intent} "
                f"phase={phase} confidence={confidence:.2f}"
            ),
        )
        return self

    def infer(self) -> DeliberationRunner:
        message = str(self._inputs.get("message") or "")
        emotion_label = str(self._inputs.get("emotion_label") or "unknown")
        intent = str(self._inputs.get("intent") or "unknown")
        self._record(
            DeliberationStageName.INFER,
            (
                f"Inferred working frame: user specified a request of {len(message)} chars; "
                f"emotion '{emotion_label}' and intent '{intent}' remain hypothesized labels"
            ),
        )
        return self

    def challenge(self, *, uncertainty: float, stress: float) -> DeliberationRunner:
        self._require(DeliberationStageName.INFER)
        action, reasons = choose_challenge_action(
            message=str(self._inputs.get("message") or ""),
            uncertainty=uncertainty,
            stress=stress,
            evidence=self.evidence,
            confidence=float(self._inputs.get("confidence") or 0.5),
        )
        if action is ChallengeAction.REQUIRE_EVIDENCE and not self.evidence:
            self.add_evidence(hypothesized_none())
        self.challenge_action = action
        self.challenge_reasons = reasons
        self._critiqued = True
        detail = f"Challenge action={action.value}"
        if reasons:
            detail += "; " + "; ".join(reasons)
        self._record(DeliberationStageName.CHALLENGE, detail)
        return self

    def simulate(self) -> DeliberationRunner:
        self._require(DeliberationStageName.INFER)
        self._critiqued = True
        if self.challenge_action is None:
            self.challenge_action = ChallengeAction.CONTINUE
        self._record(
            DeliberationStageName.SIMULATE,
            (
                "Simulated an alternative reading: the request may be exploratory "
                "rather than a committed instruction (v0 counter-reading, not a world model)"
            ),
        )
        return self

    def waive_challenge(self, reason: str) -> DeliberationRunner:
        self._require(DeliberationStageName.INFER)
        waiver = WaiverRecord(reason=_clip(reason), after_stage="infer")
        self.waivers.append(waiver)
        self._critiqued = True
        return self

    def evaluate(self, reply: str) -> DeliberationRunner:
        self._reply = reply
        claims, warnings, coverage = tag_claims(
            message=str(self._inputs.get("message") or ""),
            reply=reply,
            evidence=self.evidence,
            emotion_label=str(self._inputs.get("emotion_label") or "unknown"),
            intent=str(self._inputs.get("intent") or "unknown"),
        )
        self.claims = annotate_claims(claims, self.evidence)
        self.warnings = warnings
        self.reply_coverage = coverage
        resolution, reasons, response_commit, memory_admission = resolve_claim_gate(self.claims, self.challenge_action)
        resolution, coverage_reasons, response_commit, memory_admission = apply_coverage_gate(
            coverage, resolution, response_commit, memory_admission
        )
        memory_admission, lock_reasons = apply_write_path_lock(
            self.claims, self.evidence, memory_admission, reply=reply
        )
        self.challenge_action = resolution
        for reason in reasons + coverage_reasons + lock_reasons:
            if reason not in self.challenge_reasons:
                self.challenge_reasons.append(reason)
        self.response_commit = response_commit
        self.memory_admission = memory_admission
        self.gated_reply = apply_reply_resolution(reply, resolution, self.claims)
        from jarvis.brain.tools.claim_envelope import envelope_from_claim

        self.envelopes = [
            envelope_from_claim(claim, self.evidence).to_public_dict() for claim in self.claims
        ]
        self._record(
            DeliberationStageName.EVALUATE,
            (
                f"Evaluated {len(self.claims)} tagged claims; "
                f"{len(self.warnings)} unsupported-claim warnings; "
                f"reply coverage {self.reply_coverage.tagged_count}/{self.reply_coverage.sentence_count}; "
                f"claim-gate {resolution.value}; response={response_commit}; memory={memory_admission}"
            ),
        )
        return self

    def commit(self) -> DeliberationResult:
        if not self.evidence:
            raise DeliberationBlocked("Commit refused: no evidence reference (add a citation or 'none — hypothesized')")
        if DeliberationStageName.INFER in self._completed and not self._critiqued:
            raise DeliberationBlocked(
                "Commit refused: Infer must be followed by Challenge or Simulate unless a waiver is recorded"
            )
        for required in (
            DeliberationStageName.OBSERVE,
            DeliberationStageName.INTERPRET,
            DeliberationStageName.INFER,
            DeliberationStageName.EVALUATE,
        ):
            if required not in self._completed:
                raise DeliberationBlocked(f"Commit refused: missing {required.value} stage")
        if not self._critiqued:
            raise DeliberationBlocked(
                "Commit refused: Infer must be followed by Challenge or Simulate unless a waiver is recorded"
            )
        if self.response_commit in {"refused", "abstained"} or self.challenge_action in {
            ChallengeAction.ABSTAIN,
            ChallengeAction.BLOCK,
            ChallengeAction.FAIL_CLOSED,
        }:
            committed = False
            if self.response_commit == "abstained" or self.challenge_action is ChallengeAction.ABSTAIN:
                commit_status: Literal[
                    "committed", "blocked", "in_progress", "qualified", "revised", "refused", "abstained"
                ] = "abstained"
                self.response_commit = "abstained"
            else:
                commit_status = "refused"
                if self.response_commit == "committed":
                    self.response_commit = "refused"
        elif self.response_commit in {"qualified", "revised"}:
            commit_status = self.response_commit
            committed = True
        else:
            commit_status = "committed"
            committed = True
        if not committed:
            verb = "Abstained" if self.response_commit == "abstained" else "Refused"
        else:
            verb = "Committed"
        self._record(
            DeliberationStageName.COMMIT,
            (
                f"{verb} with {len(self.evidence)} evidence refs, "
                f"{len(self.claims)} claims, response={self.response_commit}, memory={self.memory_admission}"
            ),
        )
        return self.snapshot(status=commit_status, committed=committed)

    def snapshot(
        self,
        *,
        status: Literal[
            "committed", "blocked", "in_progress", "qualified", "revised", "refused", "abstained"
        ] = "in_progress",
        committed: bool = False,
        blocked_reason: str | None = None,
    ) -> DeliberationResult:
        return DeliberationResult(
            status=status,
            stages=list(self.stages),
            evidence=list(self.evidence),
            claims=list(self.claims),
            envelopes=list(self.envelopes),
            unsupported_claim_warnings=list(self.warnings),
            challenge_action=self.challenge_action,
            challenge_reasons=list(self.challenge_reasons),
            waivers=list(self.waivers),
            committed=committed,
            blocked_reason=blocked_reason,
            response_commit=self.response_commit,
            memory_admission=self.memory_admission,
            reply_coverage=self.reply_coverage,
        )

    def _record(self, name: DeliberationStageName, detail: str) -> None:
        self.stages.append(_stage_summary(name, detail))
        self._completed.add(name)

    def _require(self, name: DeliberationStageName) -> None:
        if name not in self._completed:
            raise DeliberationBlocked(f"{name.value} must run before this step")
