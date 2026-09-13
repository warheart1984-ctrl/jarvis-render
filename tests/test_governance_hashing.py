"""Tests for deterministic content hashes and ledger chain hashes."""

from __future__ import annotations

from jarvis.governance.hashing import (
    INTEGRITY_VERSION,
    canonicalize,
    chain_hash,
    content_hash,
    verify_chain_hash,
    verify_content_hash,
)
from jarvis.governance.schemas import ProviderMetadata


def test_content_hash_is_deterministic_for_same_input() -> None:
    payload = {"session_id": "s1", "message": "hello", "n": 2}
    assert content_hash(payload) == content_hash(payload)
    assert content_hash(payload) == content_hash({"n": 2, "message": "hello", "session_id": "s1"})


def test_content_hash_changes_when_payload_changes() -> None:
    assert content_hash({"text": "a"}) != content_hash({"text": "b"})


def test_canonicalize_is_stable_across_key_order() -> None:
    left = canonicalize({"b": 1, "a": {"z": True, "y": 0}})
    right = canonicalize({"a": {"y": 0, "z": True}, "b": 1})
    assert left == right


def test_content_hash_matches_for_pydantic_model_and_dict() -> None:
    meta = ProviderMetadata(provider="mock", model="rule-based")
    assert content_hash(meta) == content_hash(meta.model_dump(mode="json"))


def test_verify_content_hash() -> None:
    value = {"turn": 1, "input": "hi"}
    digest = content_hash(value)
    assert verify_content_hash(value, digest)
    assert not verify_content_hash({"turn": 2, "input": "hi"}, digest)


def test_chain_hash_genesis_and_successor_differ() -> None:
    payload = {"event_type": "turn_recorded", "turn_id": "s:turn:1"}
    genesis = chain_hash(None, payload)
    successor = chain_hash(genesis, payload)
    assert genesis != successor
    assert verify_chain_hash(None, payload, genesis)
    assert verify_chain_hash(genesis, payload, successor)
    assert not verify_chain_hash(None, payload, successor)


def test_chain_hash_includes_integrity_version() -> None:
    payload = {"k": "v"}
    v1 = chain_hash(None, payload, integrity_version=INTEGRITY_VERSION)
    other = chain_hash(None, payload, integrity_version="v2")
    assert v1 != other
