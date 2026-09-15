"""Replaceable SQLite persistence for Jarvis continuity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from jarvis.persistence.tenancy import LEGACY_SUBJECT, LEGACY_TENANT, ScopedLedger, migrate_scope


class JarvisStore(ScopedLedger):
    def __init__(self, path: str | Path = "jarvis.sqlite3", tenant_id: str = LEGACY_TENANT,
                 owner_sub: str = LEGACY_SUBJECT) -> None:
        super().__init__(str(path), tenant_id, owner_sub)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS sessions("
                "session_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,"
                "source_agent TEXT NOT NULL DEFAULT 'jarvis');"
                "CREATE TABLE IF NOT EXISTS spiral_turns("
                "turn_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,timestamp TEXT NOT NULL,"
                "decision TEXT NOT NULL,uncertainty REAL NOT NULL,stress REAL NOT NULL,"
                "fail_closed_reason TEXT,content_sha256 TEXT NOT NULL,content TEXT NOT NULL,"
                "evidence_json TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS memories("
                "id TEXT PRIMARY KEY,session_id TEXT NOT NULL,type TEXT NOT NULL,confidence REAL NOT NULL,"
                "evidence_json TEXT NOT NULL,status TEXT NOT NULL,content TEXT NOT NULL,subject TEXT NOT NULL,"
                "content_sha256 TEXT NOT NULL,created_at TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_turns_session ON spiral_turns(session_id,timestamp);"
                "CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id,status);"
                "CREATE TABLE IF NOT EXISTS governance_outcomes("
                "outcome_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,turn_id TEXT NOT NULL,"
                "rule_id TEXT NOT NULL,outcome TEXT NOT NULL,feedback TEXT,"
                "created_at TEXT NOT NULL,event_hash TEXT);"
                "CREATE TABLE IF NOT EXISTS evolution_reports("
                "report_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,created_at TEXT NOT NULL,"
                "report_json TEXT NOT NULL,content_sha256 TEXT NOT NULL,auto_applied INTEGER NOT NULL DEFAULT 0);"
            )
            for column, definition in {
                "provider": "TEXT NOT NULL DEFAULT 'local'",
                "model": "TEXT NOT NULL DEFAULT 'bounded-local'",
                "cost_usd": "REAL NOT NULL DEFAULT 0",
                "latency_ms": "REAL NOT NULL DEFAULT 0",
                "backend_status": "TEXT NOT NULL DEFAULT 'standalone'",
            }.items():
                try:
                    db.execute(f"ALTER TABLE spiral_turns ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            for table in ("sessions", "spiral_turns", "memories", "governance_outcomes", "evolution_reports"):
                migrate_scope(db, table)

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def save_turn(self, turn: Any) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO spiral_turns (turn_id,session_id,timestamp,decision,uncertainty,stress,fail_closed_reason,content_sha256,content,evidence_json,provider,model,cost_usd,latency_ms,backend_status,tenant_id,owner_sub) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.timestamp.isoformat(),
                    turn.decision,
                    turn.uncertainty,
                    turn.stress,
                    turn.fail_closed_reason,
                    turn.content_sha256,
                    turn.content,
                    json.dumps(turn.evidence, sort_keys=True),
                    turn.provider,
                    turn.model,
                    turn.cost_usd,
                    turn.latency_ms,
                    turn.backend_status,
                    *self.scope,
                ),
            )

    def save_turn_bundle(self, turn: Any, audit_event: dict[str, Any], memory: dict[str, Any] | None = None) -> None:
        """Atomically persist a turn, its audit event, and optional memory."""
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT event_hash FROM audit_events WHERE session_id=? AND tenant_id=? AND owner_sub=? "
                "ORDER BY rowid DESC LIMIT 1", (turn.session_id, *self.scope)
            ).fetchone()
            previous_hash = previous[0] if previous else "GENESIS"
            payload_json = json.dumps(audit_event["payload"], sort_keys=True)
            body = json.dumps(
                {
                    "event_id": audit_event["event_id"],
                    "turn_id": turn.turn_id,
                    "session_id": turn.session_id,
                    "event_type": audit_event["event_type"],
                    "timestamp": audit_event["timestamp"],
                    "payload": audit_event["payload"],
                    "previous_hash": previous_hash,
                    "tenant_id": self.tenant_id,
                    "owner_sub": self.owner_sub,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            event_hash = hashlib.sha256(body.encode()).hexdigest()
            db.execute(
                "INSERT INTO spiral_turns (turn_id,session_id,timestamp,decision,uncertainty,stress,fail_closed_reason,content_sha256,content,evidence_json,provider,model,cost_usd,latency_ms,backend_status,tenant_id,owner_sub) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.timestamp.isoformat(),
                    turn.decision,
                    turn.uncertainty,
                    turn.stress,
                    turn.fail_closed_reason,
                    turn.content_sha256,
                    turn.content,
                    json.dumps(turn.evidence, sort_keys=True),
                    turn.provider,
                    turn.model,
                    turn.cost_usd,
                    turn.latency_ms,
                    turn.backend_status,
                    *self.scope,
                ),
            )
            db.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_event["event_id"],
                    turn.turn_id,
                    turn.session_id,
                    audit_event["event_type"],
                    audit_event["timestamp"],
                    payload_json,
                    previous_hash,
                    event_hash,
                    2,
                    *self.scope,
                ),
            )
            if memory:
                db.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory["id"],
                        memory["session_id"],
                        memory.get("type", "general"),
                        memory.get("confidence", 0.5),
                        json.dumps(memory.get("evidence", [])),
                        memory.get("status", "active"),
                        memory["content"],
                        memory.get("subject", ""),
                        self.content_hash(memory["content"]),
                        memory["created_at"],
                        *self.scope,
                    ),
                )

    def load_session_turns(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM spiral_turns WHERE session_id=? AND tenant_id=? AND owner_sub=? "
                "ORDER BY timestamp DESC LIMIT ?", (session_id, *self.scope, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_memory(self, memory: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory["id"],
                    memory["session_id"],
                    memory.get("type", "general"),
                    memory.get("confidence", 0.5),
                    json.dumps(memory.get("evidence", [])),
                    memory.get("status", "active"),
                    memory["content"],
                    memory.get("subject", ""),
                    self.content_hash(memory["content"]),
                    memory["created_at"],
                    *self.scope,
                ),
            )

    def set_memory_status(self, session_id: str, memory_id: str, status: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE memories SET status=? WHERE id=? AND session_id=? AND tenant_id=? AND owner_sub=?",
                (status, memory_id, session_id, *self.scope),
            )

    def recall_memories(self, session_id: str, query: str = "") -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM memories WHERE session_id=? AND tenant_id=? AND owner_sub=? "
                "AND status='active' ORDER BY created_at DESC", (session_id, *self.scope)
            ).fetchall()
        terms = query.lower().split()
        return [dict(r) for r in rows if not terms or any(t in r["content"].lower() for t in terms)]

    def inspect_memories(self, session_id: str) -> list[dict[str, Any]]:
        """Read draft/legacy rows; callers must enforce ownership and integrity."""
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM memories WHERE session_id=? AND tenant_id=? AND owner_sub=? "
                              "ORDER BY created_at DESC", (session_id, *self.scope))
            return [dict(row) for row in rows]

    def save_governance_outcomes(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with sqlite3.connect(self.path) as db:
            db.executemany(
                "INSERT INTO governance_outcomes (outcome_id,session_id,turn_id,rule_id,outcome,"
                "feedback,created_at,event_hash,tenant_id,owner_sub) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["outcome_id"],
                        row["session_id"],
                        row["turn_id"],
                        row["rule_id"],
                        row["outcome"],
                        row.get("feedback"),
                        row["created_at"],
                        row.get("event_hash"),
                        *self.scope,
                    )
                    for row in rows
                ],
            )

    def load_governance_outcomes(self, limit: int = 200) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 2000))
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM governance_outcomes WHERE tenant_id=? AND owner_sub=? "
                "ORDER BY created_at DESC LIMIT ?",
                (*self.scope, cap),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_evolution_report(self, report: dict[str, Any], *, max_keep: int = 50) -> None:
        payload = json.dumps(report, sort_keys=True)
        keep = max(4, min(int(max_keep), 200))
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO evolution_reports (report_id,session_id,created_at,report_json,"
                "content_sha256,auto_applied,tenant_id,owner_sub) VALUES (?,?,?,?,?,?,?,?)",
                (
                    report["report_id"],
                    report.get("session_id") or "",
                    report["created_at"],
                    payload,
                    self.content_hash(payload),
                    0,
                    *self.scope,
                ),
            )
            db.execute(
                "DELETE FROM evolution_reports WHERE tenant_id=? AND owner_sub=? AND report_id IN ("
                "SELECT report_id FROM ("
                "SELECT report_id FROM evolution_reports WHERE tenant_id=? AND owner_sub=? "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?))",
                (*self.scope, *self.scope, keep),
            )

    def latest_evolution_report(self) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM evolution_reports WHERE tenant_id=? AND owner_sub=? "
                "ORDER BY created_at DESC LIMIT 1",
                self.scope,
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        body = json.loads(data["report_json"])
        body["content_sha256"] = data["content_sha256"]
        body["auto_applied"] = False
        return body
