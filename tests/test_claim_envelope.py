"""Tests for the universal claim evidence envelope (the "nerve")."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from jarvis.brain.deliberation import (
    ClaimClass,
    ClaimRecord,
    ClaimSupport,
    ClaimTag,
    EvidenceKind,
    EvidenceRef,
)
from jarvis.brain.tools.claim_envelope import (
    ArisState,
    ClaimEnvelope,
    ClaimSupportRef,
    TrustPosture,
    envelope_from_claim,
)
from jarvis.brain.tools.envelope import SourceReceipt


def _evidence(evidence_id: str = "e1") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        kind=EvidenceKind.TOOL_EXTERNAL,
        summary="description of the cite",
        authority=False,
        source="web",
    )


def _receipt() -> SourceReceipt:
    return SourceReceipt(
        source_id="nx-chunk-42",
        url="https://example.com/chunk-42",
        retrieved_at="2026-09-15T00:00:00Z",
        excerpt="a direct quote from the source",
        content_hash=hashlib.sha256(b"chunk").hexdigest(),
    )


def _factual_class() -> ClaimClass:
    return ClaimClass.FACTUAL


def _claim(evidence_ids: list[str], support: ClaimSupport | None = None) -> ClaimRecord:
    return ClaimRecord(
        claim_id="claim-reply-0",
        text="the memory exists in the ledger",
        tag=ClaimTag.HYPOTHESIZED,
        evidence_ids=evidence_ids,
        claim_class=_factual_class(),
        support=support or ClaimSupport.MISSING,
    )


def test_defaults_are_hypothesized_asserted_inspiration() -> None:
    envelope = ClaimEnvelope(claim_text="some statement")
    assert envelope.epistemic is ClaimTag.HYPOTHESIZED
    assert envelope.trust is TrustPosture.ASSERTED
    assert envelope.aris is ArisState.INSPIRATION
    assert envelope.support == []
    assert not envelope.grounded()
    assert not envelope.may_commit()
    assert envelope.receipt_id  # 64-hex sha256, derived not client-supplied


def test_receipt_id_is_deterministic_sha256() -> None:
    a = ClaimEnvelope(claim_text="same claim")
    b = ClaimEnvelope(claim_text="same claim")
    assert a.receipt_id == b.receipt_id
    assert len(a.receipt_id) == 64
    payload = json.dumps(
        {
            "claim_text": "same claim",
            "epistemic": "hypothesized",
            "trust": "asserted",
            "aris": "inspiration",
        },
        sort_keys=True,
    )
    expected_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert a.receipt_id != expected_sha  # full envelope payload, not just the visible fields


def test_proven_without_support_is_refused() -> None:
    with pytest.raises(ValidationError, match="proven claims cannot have empty support"):
        ClaimEnvelope(claim_text="claim without support", trust=TrustPosture.PROVEN)


def test_admitted_not_proven_is_refused() -> None:
    with pytest.raises(ValidationError, match="only proven claims may be admitted"):
        ClaimEnvelope(
            claim_text="inspiration not admitted",
            trust=TrustPosture.ASSERTED,
            aris=ArisState.ADMITTED,
        )


def test_proven_with_support_grounded_and_committable() -> None:
    envelope = ClaimEnvelope(
        claim_text="the ledger holds governed memories",
        support=[ClaimSupportRef.from_receipt(_receipt())],
        trust=TrustPosture.PROVEN,
        aris=ArisState.ADMITTED,
    )
    assert envelope.grounded()
    assert envelope.may_commit()
    assert envelope.receipt_id
    public = envelope.to_public_dict()
    assert public["grounded"] is True
    assert public["may_commit"] is True
    assert public["support"][0]["source_id"] == "nx-chunk-42"


def test_support_ref_from_receipt_uses_quote_and_hash() -> None:
    ref = ClaimSupportRef.from_receipt(_receipt())
    assert ref.source_id == "nx-chunk-42"
    assert ref.quote == "a direct quote from the source"
    assert ref.content_hash == hashlib.sha256(b"chunk").hexdigest()


def test_support_ref_from_evidence_ref_hashes_digest() -> None:
    ref = ClaimSupportRef.from_evidence_ref(_evidence("e1"))
    assert ref.source_id == "e1"
    assert ref.content_hash == hashlib.sha256(b"e1").hexdigest()
    assert ref.kind is EvidenceKind.TOOL_EXTERNAL


def test_envelope_from_claim_present_support_is_proven_admitted() -> None:
    claim = _claim(evidence_ids=["e1"], support=ClaimSupport.PRESENT)
    envelope = envelope_from_claim(claim, evidence=[_evidence("e1")])
    assert envelope.trust is TrustPosture.PROVEN
    assert envelope.aris is ArisState.ADMITTED
    assert envelope.grounded()
    assert envelope.may_commit()
    assert envelope.support[0].source_id == "e1"


def test_envelope_from_claim_missing_evidence_stays_asserted_inspiration() -> None:
    envelope = envelope_from_claim(
        _claim(evidence_ids=["missing"], support=ClaimSupport.MISSING), evidence=[_evidence("e1")]
    )
    # Factual claim without grounded support is REJECTED by groundedness harness
    assert envelope.trust is TrustPosture.REJECTED
    assert envelope.aris is ArisState.INSPIRATION
    assert not envelope.grounded()
    assert not envelope.may_commit()


def test_envelope_from_claim_absent_support_is_hypothesized_asserted() -> None:
    envelope = envelope_from_claim(_claim(evidence_ids=[], support=ClaimSupport.MISSING), evidence=[])
    # Factual claim without support is REJECTED
    assert envelope.trust is TrustPosture.REJECTED
    assert envelope.aris is ArisState.INSPIRATION
    assert not envelope.may_commit()


def test_may_commit_allows_hypothesized_without_support_when_waived() -> None:
    envelope = ClaimEnvelope(claim_text="hypothesis on the table")
    assert envelope.may_commit(require_support=False)
    assert not envelope.may_commit()


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        ClaimEnvelope(claim_text="some statement", bogus_field="nope")  # type: ignore[call-arg]


def test_groundedness_harness_rejects_unsupported_factual() -> None:
    from jarvis.brain.deliberation import ClaimClass
    # Factual claim with missing evidence → REJECTED
    claim = _claim(evidence_ids=["missing"], support=ClaimSupport.MISSING)
    claim.claim_class = ClaimClass.FACTUAL
    envelope = envelope_from_claim(claim, evidence=[])
    assert envelope.trust is TrustPosture.REJECTED
    assert not envelope.may_commit()
    # CAUSAL claim with missing evidence → REJECTED
    claim2 = ClaimRecord(
        claim_id="c2",
        text="A causes B",
        tag=ClaimTag.HYPOTHESIZED,
        evidence_ids=[],
        claim_class=ClaimClass.CAUSAL,
        support=ClaimSupport.MISSING,
    )
    envelope2 = envelope_from_claim(claim2, evidence=[])
    assert envelope2.trust is TrustPosture.REJECTED
    # Supported factual → PROVEN / committable
    claim3 = _claim(evidence_ids=["e1"], support=ClaimSupport.PRESENT)
    claim3.claim_class = ClaimClass.FACTUAL
    envelope3 = envelope_from_claim(claim3, evidence=[_evidence("e1")])
    assert envelope3.trust is TrustPosture.PROVEN
    assert envelope3.grounded()
    assert envelope3.may_commit()
    # Interpretive claim without support stays ASSERTED, not REJECTED
    from jarvis.brain.deliberation import ClaimClass as CC
    claim4 = ClaimRecord(
        claim_id="c4",
        text="Interpretation",
        tag=ClaimTag.HYPOTHESIZED,
        evidence_ids=[],
        claim_class=CC.INTERPRETIVE,
        support=ClaimSupport.MISSING,
    )
    envelope4 = envelope_from_claim(claim4, evidence=[])
    assert envelope4.trust is TrustPosture.ASSERTED

