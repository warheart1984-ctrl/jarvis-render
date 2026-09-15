"""Universal evidence envelope for claims.

One packet every agent emits before a claim counts. References the existing
CRS vocabulary rather than inventing a dialect: ``epistemic`` uses the DOS-lite
``ClaimTag``, support refs tie back to tool ``SourceReceipt``/``EvidenceRef``
hashes, and posture/ARIS state ride alongside. Fail-closed: an empty receipt,
proven posture without support, or an admitted claim that is not proven is refused.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.brain.deliberation import ClaimClass, ClaimRecord, ClaimSupport, ClaimTag, EvidenceKind, EvidenceRef
from jarvis.brain.tools.envelope import SourceReceipt

_CLAIM_TEXT_LIMIT = 240
_QUOTE_LIMIT = 240


class TrustPosture(str, Enum):
    ASSERTED = "asserted"
    PROVEN = "proven"
    REJECTED = "rejected"


class ArisState(str, Enum):
    INSPIRATION = "inspiration"
    PENDING_ADMISSION = "pending_admission"
    ADMITTED = "admitted"


class ClaimSupportRef(BaseModel):
    """One support reference: nx chunk id + quote, or none. Data, never authority."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=120)
    chunk_id: str | None = Field(default=None, max_length=120)
    quote: str = Field(default="", max_length=_QUOTE_LIMIT)
    content_hash: str = Field(..., min_length=1, max_length=128)
    kind: EvidenceKind = EvidenceKind.TOOL_EXTERNAL

    @field_validator("quote", "source_id", "chunk_id")
    @classmethod
    def _clip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @classmethod
    def from_receipt(cls, receipt: SourceReceipt) -> ClaimSupportRef:
        return cls(
            source_id=receipt.source_id,
            quote=receipt.excerpt,
            content_hash=receipt.content_hash,
            kind=EvidenceKind.TOOL_EXTERNAL,
        )

    @classmethod
    def from_evidence_ref(cls, ref: EvidenceRef) -> ClaimSupportRef:
        digest = ref.citation_id or ref.memory_id or ref.evidence_id
        return cls(
            source_id=ref.evidence_id,
            quote=ref.summary,
            content_hash=hashlib.sha256((digest or "none").encode("utf-8")).hexdigest()[:64],
            kind=ref.kind,
        )


class ClaimEnvelope(BaseModel):
    """One governed claim. Defaults to HYPOTHESIZED, asserted, inspiration."""

    model_config = ConfigDict(extra="forbid")

    claim_text: str = Field(..., min_length=1, max_length=_CLAIM_TEXT_LIMIT)
    epistemic: ClaimTag = ClaimTag.HYPOTHESIZED
    support: list[ClaimSupportRef] = Field(default_factory=list)
    trust: TrustPosture = TrustPosture.ASSERTED
    aris: ArisState = ArisState.INSPIRATION
    receipt_id: str = Field(default="", max_length=128)

    @field_validator("claim_text", "receipt_id")
    @classmethod
    def _clip(cls, value: str) -> str:
        return value.strip()

    @field_validator("epistemic")
    @classmethod
    def _default_hypothesized(cls, value: ClaimTag) -> ClaimTag:
        return value or ClaimTag.HYPOTHESIZED

    @model_validator(mode="after")
    def _fail_closed_posture(self) -> ClaimEnvelope:
        if self.trust is TrustPosture.PROVEN and not self.support:
            raise ValueError("proven claims cannot have empty support")
        if self.aris is ArisState.ADMITTED:
            if self.trust is not TrustPosture.PROVEN:
                raise ValueError("only proven claims may be admitted")
            if not self.support:
                raise ValueError("admitted claims cannot have empty support")
        not self.receipt_id and self._set_receipt_id()
        return self

    def _set_receipt_id(self) -> None:
        payload = {
            "claim_text": self.claim_text,
            "epistemic": self.epistemic.value,
            "support": sorted(
                [ref.model_dump(exclude={"quote"}) for ref in self.support],
                key=lambda item: item["source_id"],
            ),
            "trust": self.trust.value,
            "aris": self.aris.value,
        }
        self.receipt_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:64]

    def grounded(self) -> bool:
        return bool(self.support) and all(ref.content_hash for ref in self.support)

    def may_commit(self, *, require_support: bool = True) -> bool:
        if require_support and not self.grounded():
            return False
        if self.trust is TrustPosture.PROVEN and not self.grounded():
            return False
        if self.aris is ArisState.ADMITTED and self.trust is not TrustPosture.PROVEN:
            return False
        return True

    def to_public_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["grounded"] = self.grounded()
        data["may_commit"] = self.may_commit()
        return data


def envelope_from_claim(claim: ClaimRecord, evidence: list[EvidenceRef]) -> ClaimEnvelope:
    """Build a claim envelope from a DOS-lite claim and its available evidence.

    Support is derived from evidence ids the claim actually cites. Referenced
    evidence is treated as data (TOOL_EXTERNAL / MEMORY / HISTORY), never
    authority. Defaults stay HYPOTHESIZED / asserted / inspiration.
    Groundedness harness: FACTUAL/CAUSAL claims lacking support are REJECTED
    durably on the envelope path, not silently softened.
    """

    by_id = {item.evidence_id: item for item in evidence}
    support: list[ClaimSupportRef] = []
    for evidence_id in claim.evidence_ids:
        ref = by_id.get(evidence_id)
        if ref is None:
            continue
        support.append(ClaimSupportRef.from_evidence_ref(ref))
    # Base trust from DOS-lite support flag
    if claim.support is ClaimSupport.PRESENT and support:
        trust = TrustPosture.PROVEN
    elif claim.support is ClaimSupport.PRESENT and not support:
        # support flag present but evidence missing at envelope build time
        trust = TrustPosture.ASSERTED
    else:
        trust = TrustPosture.ASSERTED

    # Groundedness harness: fail-closed for FACTUAL/CAUSAL without support
    grounded = bool(support)
    if claim.claim_class in (ClaimClass.FACTUAL, ClaimClass.CAUSAL) and not grounded:
        trust = TrustPosture.REJECTED

    aris = ArisState.ADMITTED if trust is TrustPosture.PROVEN else ArisState.INSPIRATION
    return ClaimEnvelope(
        claim_text=claim.text,
        epistemic=claim.tag,
        support=support,
        trust=trust,
        aris=aris,
    )
