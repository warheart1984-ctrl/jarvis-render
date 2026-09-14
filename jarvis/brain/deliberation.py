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

Public traces omit secrets, hidden prompts, and private reasoning.
"""

from __future__ import annotations

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
_CLAIM_LIMIT = 12
_HYPOTHETICAL_HINTS = ("what if", "suppose", "imagine", "hypothetically")
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
    """CRS-style tagged claim. Not a proof of factual truth."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    text: str = Field(..., max_length=_SUMMARY_LIMIT)
    tag: ClaimTag
    evidence_ids: list[str] = Field(default_factory=list)
    unsupported: bool = False


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


class DeliberationResult(BaseModel):
    """Public DOS-lite envelope for traces, audit, and API metadata."""

    model_config = ConfigDict(extra="forbid")

    version: str = DOS_LITE_VERSION
    label: str = DOS_LITE_LABEL
    status: Literal["committed", "blocked", "in_progress"] = "in_progress"
    stages: list[StageRecord] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    unsupported_claim_warnings: list[str] = Field(default_factory=list)
    challenge_action: ChallengeAction | None = None
    challenge_reasons: list[str] = Field(default_factory=list)
    waivers: list[WaiverRecord] = Field(default_factory=list)
    committed: bool = False
    blocked_reason: str | None = None

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
) -> tuple[list[ClaimRecord], list[str]]:
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

    for index, sentence in enumerate(_reply_sentences(reply)):
        if len(claims) >= _CLAIM_LIMIT:
            break
        linked = _match_evidence(sentence, evidence)
        tag = ClaimTag.HYPOTHESIZED if not linked else ClaimTag.OBSERVED
        claims.append(
            ClaimRecord(
                claim_id=f"claim-reply-{index}",
                text=_clip(sentence),
                tag=tag,
                evidence_ids=linked,
                unsupported=not linked,
            )
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
    return claims[:_CLAIM_LIMIT], unique


def _reply_sentences(reply: str) -> list[str]:
    parts = [part.strip() for part in reply.replace("!", ".").replace("?", ".").split(".")]
    return [part for part in parts if len(part) > 12][:6]


def _match_evidence(sentence: str, evidence: list[EvidenceRef]) -> list[str]:
    lower = sentence.lower()
    linked: list[str] = []
    for item in evidence:
        if item.kind is EvidenceKind.HYPOTHESIZED_NONE:
            continue
        token = (item.memory_id or item.source or item.kind.value).lower()
        if token and token in lower:
            linked.append(item.evidence_id)
        elif item.kind is EvidenceKind.HISTORY and any(
            word in lower for word in ("you said", "your request", "you asked")
        ):
            linked.append(item.evidence_id)
    return linked[:4]


def challenge_reply(action: ChallengeAction) -> str | None:
    """Optional language-only override when Challenge refuses a committed answer."""

    match action:
        case ChallengeAction.CONTINUE | ChallengeAction.REQUIRE_EVIDENCE:
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
        case ChallengeAction.FAIL_CLOSED:
            return None
        case _:
            assert_never(action)


def map_challenge_decision(action: ChallengeAction, current: str) -> str:
    """Map a challenge action onto the engine decision vocabulary."""

    if current == "fail_closed":
        return current
    match action:
        case ChallengeAction.CONTINUE | ChallengeAction.REQUIRE_EVIDENCE | ChallengeAction.CLARIFY:
            return current
        case ChallengeAction.ABSTAIN:
            return "abstain"
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
        self.warnings: list[str] = []
        self.waivers: list[WaiverRecord] = []
        self.challenge_action: ChallengeAction | None = None
        self.challenge_reasons: list[str] = []
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
                )
            )
        for memory_id, summary in memories or []:
            self.add_evidence(
                EvidenceRef(
                    evidence_id=f"mem-{memory_id}",
                    kind=EvidenceKind.MEMORY,
                    summary=_clip(summary or f"memory {memory_id}"),
                    memory_id=memory_id,
                    source="memory",
                )
            )
        for index, (ref, summary) in enumerate(history or []):
            self.add_evidence(
                EvidenceRef(
                    evidence_id=f"hist-{ref or index}",
                    kind=EvidenceKind.HISTORY,
                    summary=_clip(summary or f"history {ref}"),
                    source="history",
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
        self.claims, self.warnings = tag_claims(
            message=str(self._inputs.get("message") or ""),
            reply=reply,
            evidence=self.evidence,
            emotion_label=str(self._inputs.get("emotion_label") or "unknown"),
            intent=str(self._inputs.get("intent") or "unknown"),
        )
        self._record(
            DeliberationStageName.EVALUATE,
            (f"Evaluated {len(self.claims)} tagged claims; {len(self.warnings)} unsupported-claim warnings"),
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
        self._record(
            DeliberationStageName.COMMIT,
            f"Committed with {len(self.evidence)} evidence refs and {len(self.claims)} tagged claims",
        )
        return self.snapshot(status="committed", committed=True)

    def snapshot(
        self,
        *,
        status: Literal["committed", "blocked", "in_progress"] = "in_progress",
        committed: bool = False,
        blocked_reason: str | None = None,
    ) -> DeliberationResult:
        return DeliberationResult(
            status=status,
            stages=list(self.stages),
            evidence=list(self.evidence),
            claims=list(self.claims),
            unsupported_claim_warnings=list(self.warnings),
            challenge_action=self.challenge_action,
            challenge_reasons=list(self.challenge_reasons),
            waivers=list(self.waivers),
            committed=committed,
            blocked_reason=blocked_reason,
        )

    def _record(self, name: DeliberationStageName, detail: str) -> None:
        self.stages.append(_stage_summary(name, detail))
        self._completed.add(name)

    def _require(self, name: DeliberationStageName) -> None:
        if name not in self._completed:
            raise DeliberationBlocked(f"{name.value} must run before this step")
