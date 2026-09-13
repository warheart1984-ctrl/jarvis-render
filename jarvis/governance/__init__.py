"""Governed Continuity Architecture — Phase 1 foundations.

Four distinct linked layers (do not collapse or duplicate responsibilities):

1. **Audit Trace** — human-readable structured explanation of one decision
   (facts/rules only; never private chain-of-thought, hidden prompts,
   credentials, or unnecessary sensitive memory).
2. **Audit Ledger** — append-only operational record of what happened
   (hash-chained events; insert only; privacy later via tombstone events).
3. **Continuity Ledger** — durable governed memory via a separate
   ``persistence-memory`` service over HTTP/MCP. SQLite is **not** the
   source of truth; local SQLite / in-process lists remain STM or offline
   cache only (see :class:`~jarvis.models.jarvis_types.JarvisMemoryEntry`).
4. **Reviver Ledger** — verified recovery checkpoints; never blindly
   resume from the newest unverified checkpoint.

Turn flow later phases will implement:

    User request → Audit Trace → Policy evaluation → Audit Ledger
    → Continuity Ledger → Response/action → Reviver Ledger

Policy lane precedence (first blocking lane wins; later lanes cannot override):

    Safety → Continuity/memory auth → Audit → Cost/provider
    → Drift/checkpoint → SideEffect

A blocking Safety (or earlier blocking) decision is ``fail_closed`` or
``read_only`` and cannot be loosened by Cost, Drift, or SideEffect.

Phase plan
----------
1. **This package** — schemas, deterministic hashes, PolicyLane protocol,
   composition runner, fail-closed helpers, and testable lane stubs.
2. Audit Ledger append/verify persistence against :class:`AuditLedgerEvent`.
3. :class:`ContinuityLedgerClient` HTTP/MCP implementation.
4. Reviver recovery from verified :class:`ReviverCheckpoint` records.
5. Engine/UI integration of the full loop.

Phase 1 does not persist ledgers, call Continuity over the network, or
recover sessions. Later phases should implement against these contracts
without rewriting them.
"""

from jarvis.governance.adapters import make_spiral_turn_id, policy_context_from_state, spiral_turn_from_state
from jarvis.governance.continuity import ContinuityLedgerClient, UnimplementedContinuityClient
from jarvis.governance.fail_closed import (
    STRESS_FAIL_CLOSED_THRESHOLD,
    UNCERTAINTY_FAIL_CLOSED_THRESHOLD,
    fail_closed_message,
    fail_closed_reason_for_context,
    is_audit_unavailable_fail_closed,
    is_checkpoint_integrity_fail_closed,
    is_continuity_auth_unknown_fail_closed,
    is_hash_verification_fail_closed,
    is_provider_identity_fail_closed,
    is_stress_fail_closed,
    is_uncertainty_fail_closed,
    uncertainty_from_confidence,
)
from jarvis.governance.hashing import (
    INTEGRITY_VERSION,
    canonicalize,
    chain_hash,
    content_hash,
    verify_chain_hash,
    verify_content_hash,
)
from jarvis.governance.lanes import (
    AuditPolicyLane,
    ContinuityPolicyLane,
    CostPolicyLane,
    DriftPolicyLane,
    SafetyPolicyLane,
    SideEffectPolicyLane,
    default_policy_lanes,
    evaluate_policies,
)
from jarvis.governance.policy import LANE_PRECEDENCE, PolicyLane, compose_policy_lanes
from jarvis.governance.schemas import (
    AuditLedgerEvent,
    AuditLedgerEventType,
    CompositePolicyDecision,
    ContinuityAuthStatus,
    ContinuityConflict,
    ContinuityHealth,
    ContinuityMemoryRecord,
    ContinuityProposalAck,
    ContinuityRecallQuery,
    ContinuityRecallResult,
    EvidenceRecord,
    FailClosedReason,
    MemoryProposal,
    OutcomeStatus,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyLaneName,
    ProviderMetadata,
    PublicAuditTrace,
    ReviverCheckpoint,
    SideEffectStatus,
    SpiralTurn,
    StressSignals,
    TurnDecision,
    VerificationStatus,
    build_audit_ledger_event,
    to_public_audit_trace,
)
from jarvis.governance.secrets import is_secret_key, omit_secrets

__all__ = [
    "INTEGRITY_VERSION",
    "LANE_PRECEDENCE",
    "STRESS_FAIL_CLOSED_THRESHOLD",
    "UNCERTAINTY_FAIL_CLOSED_THRESHOLD",
    "AuditLedgerEvent",
    "AuditLedgerEventType",
    "CompositePolicyDecision",
    "ContinuityAuthStatus",
    "ContinuityConflict",
    "ContinuityHealth",
    "ContinuityLedgerClient",
    "ContinuityMemoryRecord",
    "ContinuityProposalAck",
    "ContinuityRecallQuery",
    "ContinuityRecallResult",
    "EvidenceRecord",
    "FailClosedReason",
    "MemoryProposal",
    "OutcomeStatus",
    "PolicyContext",
    "PolicyDecision",
    "PolicyEffect",
    "PolicyLaneName",
    "ProviderMetadata",
    "PublicAuditTrace",
    "ReviverCheckpoint",
    "SideEffectStatus",
    "SpiralTurn",
    "StressSignals",
    "TurnDecision",
    "UnimplementedContinuityClient",
    "VerificationStatus",
    "AuditPolicyLane",
    "ContinuityPolicyLane",
    "CostPolicyLane",
    "DriftPolicyLane",
    "PolicyLane",
    "SafetyPolicyLane",
    "SideEffectPolicyLane",
    "build_audit_ledger_event",
    "canonicalize",
    "chain_hash",
    "compose_policy_lanes",
    "content_hash",
    "default_policy_lanes",
    "evaluate_policies",
    "make_spiral_turn_id",
    "fail_closed_message",
    "fail_closed_reason_for_context",
    "is_audit_unavailable_fail_closed",
    "is_checkpoint_integrity_fail_closed",
    "is_continuity_auth_unknown_fail_closed",
    "is_hash_verification_fail_closed",
    "is_provider_identity_fail_closed",
    "is_secret_key",
    "is_stress_fail_closed",
    "is_uncertainty_fail_closed",
    "omit_secrets",
    "policy_context_from_state",
    "spiral_turn_from_state",
    "to_public_audit_trace",
    "uncertainty_from_confidence",
    "verify_chain_hash",
    "verify_content_hash",
]
