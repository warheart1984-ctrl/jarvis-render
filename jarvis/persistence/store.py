"""Replaceable SQLite persistence for Jarvis continuity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class JarvisStore:
    def __init__(self, path: str | Path = "jarvis.sqlite3") -> None:
        self.path = str(path)
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

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def save_turn(self, turn: Any) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO spiral_turns (turn_id,session_id,timestamp,decision,uncertainty,stress,fail_closed_reason,content_sha256,content,evidence_json,provider,model,cost_usd,latency_ms,backend_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
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
                ),
            )

    def save_turn_bundle(self, turn: Any, audit_event: dict[str, Any], memory: dict[str, Any] | None = None) -> None:
        """Atomically persist a turn, its audit event, and optional memory."""
        with sqlite3.connect(self.path) as db:
            previous = db.execute(
                "SELECT event_hash FROM audit_events WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (turn.session_id,)
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
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            event_hash = hashlib.sha256(body.encode()).hexdigest()
            db.execute(
                "INSERT INTO spiral_turns (turn_id,session_id,timestamp,decision,uncertainty,stress,fail_closed_reason,content_sha256,content,evidence_json,provider,model,cost_usd,latency_ms,backend_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501
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
                ),
            )
            db.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    audit_event["event_id"],
                    turn.turn_id,
                    turn.session_id,
                    audit_event["event_type"],
                    audit_event["timestamp"],
                    payload_json,
                    previous_hash,
                    event_hash,
                    1,
                ),
            )
            if memory:
                db.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                    ),
                )

    def load_session_turns(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM spiral_turns WHERE session_id=? ORDER BY timestamp DESC LIMIT ?", (session_id, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_memory(self, memory: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )

    def recall_memories(self, session_id: str, query: str = "") -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM memories WHERE session_id=? AND status='active' ORDER BY created_at DESC", (session_id,)
            ).fetchall()
        terms = query.lower().split()
        return [dict(r) for r in rows if not terms or any(t in r["content"].lower() for t in terms)]
