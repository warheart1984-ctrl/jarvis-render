"""Append-only, hash-chained operational audit ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLedger:
    def __init__(self, path: str = "jarvis.sqlite3") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit_events(event_id TEXT PRIMARY KEY,turn_id TEXT,session_id TEXT NOT NULL,event_type TEXT NOT NULL,timestamp TEXT NOT NULL,payload_json TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL,integrity_version INTEGER NOT NULL)"  # noqa: E501
            )

    def append(
        self, event_id: str, session_id: str, event_type: str, payload: dict[str, Any], turn_id: str = ""
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as db:
            previous = db.execute(
                "SELECT event_hash FROM audit_events WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)
            ).fetchone()
            previous_hash = previous[0] if previous else "GENESIS"
            body = json.dumps(
                {
                    "event_id": event_id,
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "event_type": event_type,
                    "timestamp": timestamp,
                    "payload": payload,
                    "previous_hash": previous_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            event_hash = hashlib.sha256(body.encode()).hexdigest()
            db.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    turn_id,
                    session_id,
                    event_type,
                    timestamp,
                    json.dumps(payload, sort_keys=True),
                    previous_hash,
                    event_hash,
                    1,
                ),
            )
        return {"event_id": event_id, "event_hash": event_hash, "previous_hash": previous_hash}

    def list(self, session_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in db.execute("SELECT * FROM audit_events WHERE session_id=? ORDER BY rowid", (session_id,))
            ]

    def verify(self, session_id: str) -> bool:
        events = self.list(session_id)
        previous = "GENESIS"
        for e in events:
            body = json.dumps(
                {
                    "event_id": e["event_id"],
                    "turn_id": e["turn_id"],
                    "session_id": e["session_id"],
                    "event_type": e["event_type"],
                    "timestamp": e["timestamp"],
                    "payload": json.loads(e["payload_json"]),
                    "previous_hash": e["previous_hash"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if e["previous_hash"] != previous or hashlib.sha256(body.encode()).hexdigest() != e["event_hash"]:
                return False
            previous = e["event_hash"]
        return True
