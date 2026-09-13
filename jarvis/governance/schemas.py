"""Typed contracts for the four Governed Continuity layers.

These models are the Phase 1 hang-points for Audit Trace, Audit Ledger,
Continuity Ledger, and Reviver Ledger. They extend existing Jarvis/Spiral
identifiers (session id, ``SpiralPhase``, ``IntentMode``) rather than
introducing a parallel session core.

Public / exportable models forbid extra fields and strip secret-named
keys so API keys, auth headers, hidden prompts, and private reasoning
cannot leak through user-facing traces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.governance.hashing import INTEGRITY_VERSION, chain_hash, verify_chain_hash
from jarvis.governance.secrets import omit_secrets
from jarvis.models.jarvis_types import EmotionState, SpiralPhase
from jarvis.models.spiral_types import IntentMode


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnDecision(str, Enum):
    """Terminal decision recorded on a governed SpiralTurn."""

    ANSWER = "answer"
    PLAN = "plan"
    ABSTAIN = "abstain"
    FAIL_CLOSED = "fail_closed"


class OutcomeStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    FAIL_CLOSED = "fail_closed"
    ABSTAINED = "abstained"


class SideEffectStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"
    APPLIED = "applied"
    BLOCKED = "blocked"
    READ_ONLY = "read_only"


class FailClosedReason(str, Enum):
    UNCERTAINTY = "uncertainty"
    STRESS = "stress"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    CONTINUITY_AUTH_UNKNOWN = "continuity_auth_unknown"
    HASH_VERIFICATION_FAILURE = "hash_verification_failure"
    PROVIDER_IDENTITY_UNAVAILABLE = "provider_identity_unavailable"
    CHECKPOINT_INTEGRITY_FAILURE = "checkpoint_integrity_failure"


class PolicyLaneName(str, Enum):
    SAFETY = "safety"
    CONTINUITY = "continuity"
    AUDIT = "audit"
    COST = "cost"
    DRIFT = "drift"
    SIDE_EFFECT = "side_effect"


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    READ_ONLY = "read_only"
    FAIL_CLOSED = "fail_closed"


class ContinuityAuthStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    DENIED = "denied"


class VerificationStatus(str, Enum):
    """Reviver checkpoint verification. Unverified is never resume-ready."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"
    STALE = "stale"


class AuditLedgerEventType(str, Enum):
    TURN_RECORDED = "turn_recorded"
    POLICY_EVALUATED = "policy_evaluated"
    MEMORY_PROPOSED = "memory_proposed"
    TOMBSTONE = "tombstone"
    CHECKPOINT_RECORDED = "checkpoint_recorded"
    HASH_VERIFIED = "hash_verified"


class StressSignals(BaseModel):
    """Public stress / affect signals derived from :class:`EmotionState`."""

    stress: float = Field(0.0, ge=0.0, le=1.0)
    urgency: float = Field(0.0, ge=0.0, le=1.0)
    inferred_emotion: str | None = None

    @classmethod
    def from_emotion(cls, emotion: EmotionState) -> StressSignals:
        return cls(
            stress=emotion.stress,
            urgency=emotion.urgency,
            inferred_emotion=emotion.inferred_emotion,
        )


class EvidenceRecord(BaseModel):
    """A fact or rule cited by the Audit Trace — not private reasoning."""

    evidence_id: str
    source: str
    summary: str
    memory_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _omit_secret_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return omit_secrets(value)


class ProviderMetadata(BaseModel):
    """Provider/model identity for a turn. Never includes API keys."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    model_version: str | None = None
    request_id: str | None = None


class PolicyDecision(BaseModel):
    """Result of one policy lane evaluation."""

    lane: PolicyLaneName
    effect: PolicyEffect
    blocking: bool
    reason: str = ""
    fail_closed_reason: FailClosedReason | None = None

    @model_validator(mode="after")
    def _blocking_matches_effect(self) -> PolicyDecision:
        if self.blocking and self.effect is PolicyEffect.ALLOW:
            raise ValueError("blocking policy decisions must be fail_closed or read_only")
        if self.effect is PolicyEffect.FAIL_CLOSED and not self.blocking:
            raise ValueError("fail_closed decisions must be blocking")
        if self.effect is PolicyEffect.READ_ONLY and not self.blocking:
            raise ValueError("read_only decisions must be blocking")
        return self


class PolicyContext(BaseModel):
    """Signals evaluated by policy lanes for one SpiralTurn."""

    session_id: str
    turn_id: str
    uncertainty: float = Field(0.0, ge=0.0, le=1.0)
    stress: float = Field(0.0, ge=0.0, le=1.0)
    audit_available: bool = True
    continuity_auth: ContinuityAuthStatus = ContinuityAuthStatus.KNOWN
    hash_verified: bool = True
    provider_identity_available: bool = True
    checkpoint_integrity_ok: bool = True
    side_effects_requested: bool = False
    side_effects_authorized: bool = False
    proposed_decision: TurnDecision = TurnDecision.ANSWER


class CompositePolicyDecision(BaseModel):
    """Precedence-resolved policy outcome plus per-lane results."""

    effect: PolicyEffect
    blocking: bool
    fail_closed_reason: FailClosedReason | None = None
    blocking_lane: PolicyLaneName | None = None
    lane_decisions: list[PolicyDecision] = Field(default_factory=list)
    proposed_decision: TurnDecision = TurnDecision.ANSWER

    @property
    def suggested_turn_decision(self) -> TurnDecision:
        if self.effect is PolicyEffect.FAIL_CLOSED:
            return TurnDecision.FAIL_CLOSED
        if self.effect is PolicyEffect.READ_ONLY:
            return TurnDecision.ABSTAIN
        if self.effect is PolicyEffect.ALLOW:
            return self.proposed_decision
        assert_never(self.effect)

    @property
    def side_effect_status(self) -> SideEffectStatus:
        if self.effect is PolicyEffect.FAIL_CLOSED:
            return SideEffectStatus.BLOCKED
        if self.effect is PolicyEffect.READ_ONLY:
            return SideEffectStatus.READ_ONLY
        if self.effect is PolicyEffect.ALLOW:
            return SideEffectStatus.NONE
        assert_never(self.effect)


class SpiralTurn(BaseModel):
    """One governed Jarvis/Spiral turn, including Audit Trace fields.

    This is the structured record later phases attach to the chat loop.
    It is not a substitute for :class:`~jarvis.models.jarvis_types.JarvisState`;
    it explains a single decision on top of that session state.
    """

    turn_id: str
    session_id: str
    input_hash: str
    output_hash: str
    retrieved_memory_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    uncertainty: float = Field(..., ge=0.0, le=1.0)
    stress_signals: StressSignals = Field(default_factory=StressSignals)
    policy_lanes_evaluated: list[PolicyLaneName] = Field(default_factory=list)
    provider: ProviderMetadata | None = None
    decision: TurnDecision
    fail_closed_reason: FailClosedReason | None = None
    side_effect_status: SideEffectStatus = SideEffectStatus.NONE
    outcome_status: OutcomeStatus = OutcomeStatus.OK
    error_status: str | None = None
    facts: list[str] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)
    phase: SpiralPhase | None = None
    intent: IntentMode | None = None
    created_at: str = Field(default_factory=_utc_now)

    def to_public_audit_trace(self) -> PublicAuditTrace:
        return PublicAuditTrace.from_turn(self)


class PublicAuditTrace(BaseModel):
    """User-facing Audit Trace for one decision.

    Facts and rules only. Extra fields are rejected so hidden prompts,
    credentials, and private CoT cannot be smuggled onto the export.
    """

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    session_id: str
    input_hash: str
    output_hash: str
    retrieved_memory_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    uncertainty: float = Field(..., ge=0.0, le=1.0)
    stress_signals: StressSignals
    policy_lanes_evaluated: list[PolicyLaneName] = Field(default_factory=list)
    provider: ProviderMetadata | None = None
    decision: TurnDecision
    fail_closed_reason: FailClosedReason | None = None
    side_effect_status: SideEffectStatus
    outcome_status: OutcomeStatus
    error_status: str | None = None
    facts: list[str] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)
    phase: SpiralPhase | None = None
    intent: IntentMode | None = None

    @classmethod
    def from_turn(cls, turn: SpiralTurn) -> PublicAuditTrace:
        evidence = [
            EvidenceRecord(
                evidence_id=item.evidence_id,
                source=item.source,
                summary=item.summary,
                memory_id=item.memory_id,
                metadata=omit_secrets(item.metadata),
            )
            for item in turn.evidence
        ]
        return cls(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            input_hash=turn.input_hash,
            output_hash=turn.output_hash,
            retrieved_memory_ids=list(turn.retrieved_memory_ids),
            evidence=evidence,
            confidence=turn.confidence,
            uncertainty=turn.uncertainty,
            stress_signals=turn.stress_signals,
            policy_lanes_evaluated=list(turn.policy_lanes_evaluated),
            provider=turn.provider,
            decision=turn.decision,
            fail_closed_reason=turn.fail_closed_reason,
            side_effect_status=turn.side_effect_status,
            outcome_status=turn.outcome_status,
            error_status=turn.error_status,
            facts=list(turn.facts),
            rules_applied=list(turn.rules_applied),
            phase=turn.phase,
            intent=turn.intent,
        )


def to_public_audit_trace(turn: SpiralTurn) -> PublicAuditTrace:
    """Sanitized Audit Trace view of a :class:`SpiralTurn`."""

    return PublicAuditTrace.from_turn(turn)


class AuditLedgerEvent(BaseModel):
    """Append-only Audit Ledger event skeleton (hash-chained).

    Distinct from the Spiral V8 :class:`~jarvis.models.spiral_types.EventEnvelope`.
    Phase 2 implements persistence and chain verification; Phase 1 records
    the contract including ``previous_event_hash`` / ``current_event_hash``.
    """

    event_id: str
    turn_id: str
    session_id: str
    event_type: AuditLedgerEventType
    timestamp: str = Field(default_factory=_utc_now)
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    previous_event_hash: str | None = None
    current_event_hash: str
    integrity_version: str = INTEGRITY_VERSION

    @field_validator("normalized_payload")
    @classmethod
    def _omit_secret_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return omit_secrets(value)

    def verify_integrity(self) -> bool:
        return verify_chain_hash(
            self.previous_event_hash,
            self._hash_payload(),
            self.current_event_hash,
            integrity_version=self.integrity_version,
        )

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "normalized_payload": self.normalized_payload,
        }


def build_audit_ledger_event(
    *,
    event_id: str,
    turn_id: str,
    session_id: str,
    event_type: AuditLedgerEventType,
    payload: dict[str, Any],
    previous_event_hash: str | None,
    timestamp: str | None = None,
    integrity_version: str = INTEGRITY_VERSION,
) -> AuditLedgerEvent:
    """Construct an event with secrets omitted and the chain hash filled in."""

    normalized = omit_secrets(payload)
    event = AuditLedgerEvent(
        event_id=event_id,
        turn_id=turn_id,
        session_id=session_id,
        event_type=event_type,
        timestamp=timestamp or _utc_now(),
        normalized_payload=normalized,
        previous_event_hash=previous_event_hash,
        current_event_hash="pending",
        integrity_version=integrity_version,
    )
    event.current_event_hash = chain_hash(
        previous_event_hash, event._hash_payload(), integrity_version=integrity_version
    )
    return event


class ContinuityRecallQuery(BaseModel):
    session_id: str
    query: str
    limit: int = Field(10, ge=1, le=100)


class ContinuityMemoryRecord(BaseModel):
    """Governed memory record returned by Continuity — summaries, not secrets."""

    memory_id: str
    session_id: str
    summary: str
    version: str | None = None
    content_hash: str | None = None


class ContinuityRecallResult(BaseModel):
    memory_ids: list[str] = Field(default_factory=list)
    records: list[ContinuityMemoryRecord] = Field(default_factory=list)


class MemoryProposal(BaseModel):
    session_id: str
    turn_id: str
    content_hash: str
    summary: str
    category: str = "general"


class ContinuityProposalAck(BaseModel):
    accepted: bool
    memory_id: str | None = None
    status: str = "accepted"


class ContinuityConflict(BaseModel):
    conflict_id: str
    session_id: str
    memory_ids: list[str] = Field(default_factory=list)
    summary: str


class ContinuityHealth(BaseModel):
    """Health of the remote persistence-memory service (not local SQLite)."""

    status: str
    service: str = "persistence-memory"
    sqlite_is_authority: Literal[False] = False


class ReviverCheckpoint(BaseModel):
    """Verified recovery checkpoint skeleton.

    Stores references (hashes, cursors, provider config ref) — never API
    keys. Phase 4 must refuse to resume when ``verification_status`` is
    not ``verified``.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    session_id: str
    last_verified_turn_id: str | None = None
    audit_ledger_hash: str
    continuity_cursor: str | None = None
    continuity_version: str | None = None
    serialized_state_refs: list[str] = Field(default_factory=list)
    pending_task_state: dict[str, Any] = Field(default_factory=dict)
    provider_config_ref: str
    created_at: str = Field(default_factory=_utc_now)
    verified_at: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    @field_validator("pending_task_state")
    @classmethod
    def _omit_secret_task_state(cls, value: dict[str, Any]) -> dict[str, Any]:
        return omit_secrets(value)

    @property
    def is_resume_ready(self) -> bool:
        return self.verification_status is VerificationStatus.VERIFIED
