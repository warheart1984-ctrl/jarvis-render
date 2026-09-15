"""Tenant A cannot read tenant B's sessions, recall, or audit — even with the same user_id label."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from jarvis.auth import AccessStore
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult
from jarvis.core.config import settings
from jarvis.identity import Principal as LedgerPrincipal
from jarvis.identity import tenant_key
from jarvis.models.jarvis_types import ChatRequest
from jarvis.persistence import AuditLedger, JarvisStore, ReviverLedger
from jarvis.persistence.recall import RecallLedger
from jarvis.persistence.tenancy import LEGACY_SUBJECT, LEGACY_TENANT

KEY = "test-service-token"
ALICE = ("t_alice", "google-sub-alice")
BOB = ("t_bob", "google-sub-bob")


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "service_token", KEY)
    monkeypatch.setattr(settings, "recall_owner_user_id", "owner")
    monkeypatch.setattr(settings, "recall_signing_key", "tenant-master-key")
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("A contextual answer.", "test", "text-model", 1)),
    )
    path = tmp_path / "tenants.sqlite3"
    alice = JarvisEngine(store=JarvisStore(path, *ALICE))
    bob = JarvisEngine(store=JarvisStore(path, *BOB))
    return alice, bob


async def turn(engine: JarvisEngine, message: str, **kwargs):
    return await engine.chat(ChatRequest(user_id="owner", message=message, **kwargs), recall_owner="owner")


def test_google_subjects_mint_separate_personal_tenants(tmp_path: Path):
    access = AccessStore(str(tmp_path / "auth.sqlite3"))
    first, _ = access.login("sub-alice", "shared@gmail.com")
    second, _ = access.login("sub-bob", "shared@gmail.com")
    renamed, token = access.login("sub-alice", "new-alice@gmail.com")
    assert first.tenant_id != second.tenant_id
    assert first.user_id != second.user_id
    assert first.user_id.startswith("oidc-")
    assert renamed.user_id == first.user_id
    assert renamed.tenant_id == first.tenant_id
    assert renamed.email == "new-alice@gmail.com"
    assert renamed.recall_key() != second.recall_key()
    assert access.authenticate(token) is None


def test_ledger_namespace_is_issuer_and_subject_not_email():
    alice = LedgerPrincipal("sub-alice", frozenset({"memory.read"}), "https://accounts.google.com")
    bob = LedgerPrincipal("sub-bob", frozenset({"memory.read"}), "https://accounts.google.com")
    renamed = LedgerPrincipal("sub-alice", frozenset({"memory.read"}), "https://accounts.google.com")
    assert tenant_key(alice) == hashlib.sha256(b"https://accounts.google.com\x1fsub-alice").hexdigest()
    assert tenant_key(alice) != tenant_key(bob)
    assert tenant_key(alice) == tenant_key(renamed)


@pytest.mark.asyncio
async def test_two_google_subjects_cannot_share_recall_or_session_rows(isolated):
    alice, bob = isolated
    source = await turn(alice, "ALICE_PRIVATE_FACT")
    later = await turn(alice, "What did we talk about last session?", recall_previous=True)
    recalled = alice.recall.previous("owner", KEY, session_id=later.session_id, before="9999")

    assert later.previous_session["status"] == "verified"
    assert later.previous_session["source_session_id"] == source.session_id
    assert recalled.state is not None
    assert "ALICE_PRIVATE_FACT" in json.dumps(recalled.state.model_dump())
    assert alice.store.load_session_turns(source.session_id)
    assert alice.audit.verify(source.session_id)
    assert alice.reviver.recover(source.session_id, alice.audit) is not None

    assert bob.store.load_session_turns(source.session_id) == []
    assert bob.store.inspect_memories(source.session_id) == []
    assert bob.audit.list(source.session_id) == []
    assert bob.audit.verify(source.session_id) is False
    assert bob.reviver.recover(source.session_id, bob.audit) is None
    foreign = bob.recall.previous("owner", KEY, session_id="bob-current", before="9999")
    assert foreign.state is None
    assert foreign.metadata["status"] == "no_eligible_history"
    assert "ALICE_PRIVATE_FACT" not in json.dumps(foreign.metadata)

    stolen = await turn(bob, "What did Alice say?", recall_previous=True)
    assert stolen.previous_session["status"] == "no_eligible_history"
    assert "ALICE_PRIVATE_FACT" not in json.dumps(stolen.model_dump())


@pytest.mark.asyncio
async def test_service_token_mac_does_not_cross_tenant_attestation(isolated):
    alice, bob = isolated
    source = await turn(alice, "ALICE_SIGNED_CHECKPOINT")
    alice_row = alice.recall.previous("owner", KEY, session_id="other", before="9999")
    assert alice_row.metadata["status"] == "verified"
    assert alice_row.metadata["tenant_id"] == "t_alice"
    assert alice_row.metadata["owner_sub"] == "google-sub-alice"

    with pytest.raises(ValueError, match="Checkpoint or audit could not be verified"):
        bob.recall.attest(source.session_id, "owner", KEY)

    record = dict(
        next(row for row in _rows(alice.store.path, "recall_checkpoints") if row["session_id"] == source.session_id)
    )
    assert alice.recall.valid_signature(record, KEY)
    assert not bob.recall.valid_signature(record, KEY)
    assert not bob.recall.valid_signature({**record, "signature_version": 1}, KEY)
    legacy = RecallLedger(alice.store.path)
    assert legacy.scope == (LEGACY_TENANT, LEGACY_SUBJECT)
    assert legacy.previous("owner", KEY, session_id="other", before="9999").state is None


def _rows(path: str, table: str) -> list[dict]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def test_unknown_or_foreign_audit_verify_is_absent_not_valid(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "service_token", KEY)
    monkeypatch.setattr(settings, "environment", "production")
    import jarvis.main as main
    import jarvis.routes.chat as chat
    import jarvis.routes.voice as voice

    engine = JarvisEngine(store=JarvisStore(tmp_path / "api.sqlite3", *ALICE))
    for mod in (main, chat, voice):
        monkeypatch.setattr(mod, "engine", engine)
    main._rate.clear()
    with TestClient(main.app) as client:
        headers = {"X-Jarvis-Service-Token": KEY}
        missing = client.get("/sessions/does-not-exist/audit/verify", headers=headers)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Session not found"}
        AuditLedger(engine.store.path, *BOB).append("e1", "does-not-exist", "spiral_turn", {})
        ReviverLedger(engine.store.path, *BOB).save(
            "c1", "does-not-exist", "missing", "hash", {"session_id": "does-not-exist"}, verified=True
        )
        still_missing = client.get("/sessions/does-not-exist/audit/verify", headers=headers)
        assert still_missing.status_code == 404
