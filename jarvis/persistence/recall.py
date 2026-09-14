"""Read-only, operator-scoped recall of explicitly attested checkpoints.

The service token is a single-operator credential, NOT multi-user authentication.
Legacy checkpoints require explicit operator attestation; reads never enroll them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jarvis.models.jarvis_types import JarvisState
from jarvis.persistence.audit import AuditLedger
from jarvis.persistence.reviver import ReviverLedger
from jarvis.persistence.tenancy import LEGACY_SUBJECT, LEGACY_TENANT, ScopedLedger, migrate_scope


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def signature(value: Any, key: str) -> str:
    return hmac.new(key.encode(), ("jarvis-recall-v1\n" + canonical(value)).encode(), hashlib.sha256).hexdigest()


@dataclass
class RecallResult:
    metadata: dict[str, Any] = field(default_factory=lambda: {"status": "disabled"})
    state: JarvisState | None = None


class RecallLedger(ScopedLedger):
    def __init__(self, path: str, tenant_id: str = LEGACY_TENANT, owner_sub: str = LEGACY_SUBJECT) -> None:
        super().__init__(path, tenant_id, owner_sub)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS recall_checkpoints ("
                "checkpoint_id TEXT NOT NULL, key_id TEXT NOT NULL, owner TEXT NOT NULL, "
                "session_id TEXT NOT NULL, created_at TEXT NOT NULL, state_sha256 TEXT NOT NULL, "
                "audit_hash TEXT NOT NULL, origin TEXT NOT NULL, signature TEXT NOT NULL, "
                "PRIMARY KEY(checkpoint_id,key_id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS recall_owner_time ON recall_checkpoints(owner, created_at DESC)")
            db.execute("CREATE TABLE IF NOT EXISTS recall_revoked(session_id TEXT PRIMARY KEY)")
            migrate_scope(db, "recall_checkpoints")
            migrate_scope(db, "recall_revoked")
            if "signature_version" not in {r[1] for r in db.execute("PRAGMA table_info(recall_checkpoints)")}:
                db.execute("ALTER TABLE recall_checkpoints ADD COLUMN signature_version INTEGER NOT NULL DEFAULT 1")

    def valid_signature(self, row: dict, key: str) -> bool:
        record = dict(row)
        supplied = record.pop("signature")
        version = record.get("signature_version", 1)
        if version == 1:
            if self.scope != (LEGACY_TENANT, LEGACY_SUBJECT):
                return False
            for field in ("signature_version", "tenant_id", "owner_sub"):
                record.pop(field, None)
        elif version != 2:
            return False
        elif (record.get("tenant_id"), record.get("owner_sub")) != self.scope:
            return False
        return hmac.compare_digest(supplied, signature(record, key))

    def attest(
        self, session_id: str, owner: str, key: str, *, expected_state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Trusted server/operator call only; never exposed as a public endpoint.

        Omitting expected_state explicitly attests legacy data as it exists NOW;
        this cannot establish integrity before the attestation time.
        """
        if not key or not owner:
            raise ValueError("Recall requires a service token and a server-bound owner")
        checkpoint = ReviverLedger(self.path, *self.scope).recover(session_id, AuditLedger(self.path, *self.scope))
        if checkpoint is None:
            raise ValueError("Checkpoint or audit could not be verified")
        state = JarvisState.model_validate(checkpoint["state"])
        if state.user_id != owner or state.session_id != session_id:
            raise ValueError("Checkpoint ownership mismatch")
        if expected_state is not None and canonical(checkpoint["state"]) != canonical(expected_state):
            raise ValueError("Checkpoint differs from the state being saved")
        record = {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "key_id": hashlib.sha256(key.encode()).hexdigest(),
            "owner": owner,
            "session_id": session_id,
            "created_at": checkpoint["created_at"],
            "state_sha256": hashlib.sha256(canonical(checkpoint["state"]).encode()).hexdigest(),
            "audit_hash": checkpoint["audit_hash"],
            "origin": "authenticated_turn" if expected_state is not None else "operator_attested_legacy",
            "tenant_id": self.tenant_id,
            "owner_sub": self.owner_sub,
            "signature_version": 2,
        }
        record["signature"] = signature(record, key)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute(
                "SELECT * FROM recall_checkpoints WHERE checkpoint_id=? AND key_id=? AND tenant_id=? AND owner_sub=?",
                (record["checkpoint_id"], record["key_id"], *self.scope),
            ).fetchone()
            if existing:
                # Idempotent even when an operator re-attests a newly signed turn.
                prior = dict(existing)
                if not self.valid_signature(prior, key) or any(
                    prior[k] != record[k] for k in prior if k not in {"origin", "signature", "signature_version"}
                ):
                    raise ValueError("Immutable recall attestation mismatch")
                return dict(existing)
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            db.execute(f"INSERT INTO recall_checkpoints ({columns}) VALUES ({placeholders})", tuple(record.values()))
        return record

    def revoke(self, session_id: str) -> None:
        """A clear-memory request permanently excludes this source from recall."""
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO recall_revoked VALUES (?,?,?)", (session_id, *self.scope))

    def previous(self, owner: str, key: str, *, session_id: str, before: str) -> RecallResult:
        if not key or not owner:
            return RecallResult()
        try:
            with sqlite3.connect(self.path) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT * FROM recall_checkpoints WHERE owner=? AND session_id!=? AND created_at<? "
                    "AND tenant_id=? AND owner_sub=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (owner, session_id, before, *self.scope),
                ).fetchone()
                if not row:
                    return RecallResult({"status": "no_eligible_history"})
                record = dict(row)
                if not self.valid_signature(record, key):
                    return RecallResult({"status": "unverified"})
                if db.execute("SELECT 1 FROM recall_revoked WHERE session_id=? AND tenant_id=? AND owner_sub=?",
                              (record["session_id"], *self.scope)).fetchone():
                    return RecallResult({"status": "withheld"})
            checkpoint = ReviverLedger(self.path, *self.scope).recover(
                record["session_id"], AuditLedger(self.path, *self.scope)
            )
            if checkpoint is None or any(
                checkpoint[k] != record[k] for k in ("checkpoint_id", "session_id", "created_at", "audit_hash")
            ):
                return RecallResult({"status": "unverified"})
            digest = hashlib.sha256(canonical(checkpoint["state"]).encode()).hexdigest()
            state = JarvisState.model_validate(checkpoint["state"])
            if digest != record["state_sha256"] or state.user_id != owner or state.session_id != record["session_id"]:
                return RecallResult({"status": "unverified"})
            return RecallResult(
                {
                    "status": "verified",
                    "source_session_id": state.session_id,
                    "checkpoint_id": record["checkpoint_id"],
                    "audit_hash": record["audit_hash"],
                    "source_updated_at": record["created_at"],
                    "attestation": record["origin"],
                    "tenant_id": self.tenant_id,
                    "owner_sub": self.owner_sub,
                    "read_only": True,
                },
                state,
            )
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            # No content or alternate session on a damaged/missing dependency.
            return RecallResult({"status": "unavailable"})

    def is_revoked(self, session_id: str) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM recall_revoked WHERE session_id=? AND tenant_id=? AND owner_sub=?",
                              (session_id, *self.scope)).fetchone() is not None


def main() -> None:
    """Explicit local migration; session IDs are required and no secrets are printed."""
    import argparse

    from jarvis.core.config import settings

    parser = argparse.ArgumentParser(description="Attest explicitly reviewed legacy Jarvis sessions for recall")
    parser.add_argument("session_ids", nargs="+")
    args = parser.parse_args()
    ledger = RecallLedger(settings.memory_db_path)
    for session_id in args.session_ids:
        record = ledger.attest(session_id, settings.recall_owner_user_id, settings.service_token)
        print(canonical({k: record[k] for k in ("session_id", "checkpoint_id", "origin")}))


if __name__ == "__main__":
    main()
