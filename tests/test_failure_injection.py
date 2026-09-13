from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jarvis.continuity import ContinuityLedgerClient, WriteStatus
from jarvis.persistence.audit import AuditLedger


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    ledger = AuditLedger(tmp_path / "audit.sqlite3")
    ledger.append("e1", "s1", "turn", {"decision": "answer"})
    assert ledger.verify("s1") is True
    with __import__("sqlite3").connect(tmp_path / "audit.sqlite3") as db:
        db.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id='e1'", (json.dumps({"decision": "fail_closed"}),)
        )
    assert ledger.verify("s1") is False


def test_audit_chains_are_session_scoped(tmp_path: Path) -> None:
    ledger = AuditLedger(tmp_path / "audit.sqlite3")
    ledger.append("a", "one", "turn", {})
    ledger.append("b", "two", "turn", {})
    ledger.append("c", "one", "turn", {})
    assert ledger.verify("one") is True
    assert ledger.verify("two") is True


@pytest.mark.asyncio
async def test_continuity_500_is_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "injected failure"})

    client = ContinuityLedgerClient("http://ledger")
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[assignment]
    try:
        result = await client.propose_memory({"content": "x", "session_id": "s", "user_requested": True}, "k")
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]
    assert result["status"] == WriteStatus.UNAVAILABLE.value


@pytest.mark.asyncio
async def test_continuity_retry_reuses_idempotency_key() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"accepted": True, "memory": {"id": "m1"}})

    client = ContinuityLedgerClient("http://ledger")
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[assignment]
    try:
        result = await client.propose_memory({"content": "x", "session_id": "s", "user_requested": True}, "stable-key")
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]
    assert result["status"] == WriteStatus.ACCEPTED.value
    assert seen == ["stable-key", "stable-key"]


@pytest.mark.asyncio
async def test_continuity_refused_conflict_is_not_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accepted": False,
                "refused": True,
                "refuse_reason": "conflict-membrane",
                "conflicts": [{"id": "m1"}],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as raw:
        response = await raw.post("http://ledger/api/jarvis/tools/emr_remember", json={"x": 1})
    assert response.status_code == 200
    assert response.json()["refuse_reason"] == "conflict-membrane"
