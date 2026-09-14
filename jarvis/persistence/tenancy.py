"""Additive tenant migration; old signed payloads are never rewritten.

The existing single-operator vault is labeled t_jon / operator-legacy, without
claiming a Google subject owns it. All new persistence uses explicit scope.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

LEGACY_TENANT = "t_jon"
LEGACY_SUBJECT = "operator-legacy"
TABLES = frozenset({"sessions", "spiral_turns", "memories", "audit_events", "reviver_checkpoints",
                    "recall_checkpoints", "recall_revoked"})


def migrate_scope(db: sqlite3.Connection, table: str) -> None:
    if table not in TABLES:
        raise ValueError("Unknown scoped table")
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if "tenant_id" not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{LEGACY_TENANT}'")
    if "owner_sub" not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN owner_sub TEXT NOT NULL DEFAULT '{LEGACY_SUBJECT}'")
    db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id,owner_sub,session_id)")


class ScopedLedger:
    def __init__(self, path: str, tenant_id: str = LEGACY_TENANT, owner_sub: str = LEGACY_SUBJECT) -> None:
        if not tenant_id or not owner_sub:
            raise ValueError("Storage requires a tenant and subject")
        self.path = str(path)
        self.tenant_id, self.owner_sub = tenant_id, owner_sub

    @property
    def scope(self) -> tuple[str, str]:
        return self.tenant_id, self.owner_sub

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()
