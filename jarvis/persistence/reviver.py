"""Verified restart checkpoints for Jarvis."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


class ReviverLedger:
    def __init__(self, path: str = "jarvis.sqlite3") -> None:
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviver_checkpoints(checkpoint_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,turn_id TEXT NOT NULL,audit_hash TEXT NOT NULL,state_json TEXT NOT NULL,created_at TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0)"  # noqa: E501
            )

    def save(
        self,
        checkpoint_id: str,
        session_id: str,
        turn_id: str,
        audit_hash: str,
        state: dict[str, Any],
        verified: bool = False,
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO reviver_checkpoints VALUES (?,?,?,?,?,?,?)",
                (
                    checkpoint_id,
                    session_id,
                    turn_id,
                    audit_hash,
                    json.dumps(state, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                    int(verified),
                ),
            )

    def latest_verified(self, session_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM reviver_checkpoints WHERE session_id=? AND verified=1 ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def recover(self, session_id: str, audit: Any) -> dict[str, Any] | None:
        """Return the newest checkpoint only when its session audit is intact.

        A checkpoint flag is not sufficient evidence: operators or a damaged
        database could have marked it verified without a valid audit chain.
        Recovery therefore fails closed when the chain cannot be verified.
        """
        if not audit.verify(session_id):
            return None
        checkpoint = self.latest_verified(session_id)
        if checkpoint is None:
            return None
        event = next(
            (
                item
                for item in reversed(audit.list(session_id))
                if item["turn_id"] == checkpoint["turn_id"] and item["event_type"] == "spiral_turn"
            ),
            None,
        )
        if event is None or event["event_hash"] != checkpoint["audit_hash"]:
            return None
        checkpoint["state"] = json.loads(checkpoint.pop("state_json"))
        return checkpoint
