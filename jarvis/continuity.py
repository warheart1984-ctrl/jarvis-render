"""Explicit, governed client for the external Continuity Ledger."""

from __future__ import annotations

import asyncio
import hashlib
from enum import StrEnum
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel


class WriteStatus(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    SIMULATED = "simulated"


class GovernedWriteResult(BaseModel):
    protocol: str = "emr-write-v1"
    accepted: bool = False
    refused: bool = False
    refuse_reason: str | None = None
    refuse_detail: str | None = None
    memory: dict[str, Any] | None = None
    lineage: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = []
    status: WriteStatus = WriteStatus.UNKNOWN
    transaction_id: str = ""
    correlation_id: str = ""


class ContinuityLedgerClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout

    async def propose_memory(self, memory: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not memory.get("user_requested"):
            raise ValueError("Durable memory writes require explicit user_requested=true")
        transaction_id, correlation_id = uuid4().hex, uuid4().hex
        if memory.get("dry_run") is True:
            return GovernedWriteResult(
                status=WriteStatus.SIMULATED,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                refuse_detail="Dry-run: no external write performed",
            ).model_dump()
        payload = {
            "content": memory["content"],
            "source_agent": "jarvis",
            "session_id": memory["session_id"],
            "type": memory.get("type", "fact"),
            "subject": memory.get("subject", ""),
            "user_requested": True,
            "user_statement": memory.get("user_statement", memory["content"]),
        }
        payload.update(
            {"transaction_id": transaction_id, "correlation_id": correlation_id, "idempotency_key": idempotency_key}
        )
        try:
            data = await self._post_with_retries(
                "/api/jarvis/tools/emr_remember",
                payload,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            return GovernedWriteResult(
                status=WriteStatus.UNAVAILABLE,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                refuse_detail=str(exc),
            ).model_dump()
        return self._normalize_write_result(data, transaction_id=transaction_id, correlation_id=correlation_id)

    async def recall(self, session_id: str, query: str, intent: str = "transform") -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/jarvis/tools/emr_recall",
                json={"intent": intent, "query": query},
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()

    async def retrieve(self, memory_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/api/jarvis/memory/{memory_id}", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def supersede(self, memory_id: str, content: str, session_id: str) -> dict[str, Any]:
        idempotency_key = f"jarvis-supersede-{memory_id}-{self._content_hash(content)}"
        transaction_id, correlation_id = uuid4().hex, uuid4().hex
        payload = {
            "id": memory_id,
            "content": content,
            "supersedes": memory_id,
            "source_agent": "jarvis",
            "session_id": session_id,
            "type": "architecture",
            "subject": "jarvis-prototype",
            "user_requested": True,
            "user_statement": "User explicitly superseded this memory.",
            "transaction_id": transaction_id,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
        }
        try:
            data = await self._post_with_retries(
                "/api/jarvis/tools/emr_upsert",
                payload,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            return GovernedWriteResult(
                status=WriteStatus.UNAVAILABLE,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                refuse_detail=str(exc),
            ).model_dump()
        return self._normalize_write_result(data, transaction_id=transaction_id, correlation_id=correlation_id)

    async def _post_with_retries(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            data = None
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=payload,
                        headers={
                            **self.headers,
                            "X-Request-ID": correlation_id,
                            "Idempotency-Key": idempotency_key,
                        },
                    )
                    # Retry only transient server/rate-limit failures. A
                    # refusal or conflict is a durable business decision.
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        response.raise_for_status()
                        data = response.json()
                        break
                    if attempt == 2:
                        response.raise_for_status()
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 2:
                        raise
                await asyncio.sleep(0.05 * (2**attempt))
            if data is None:
                raise RuntimeError("Continuity Ledger did not return a response")
            return data

    @staticmethod
    def _normalize_write_result(
        data: dict[str, Any],
        *,
        transaction_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        if data.get("refused"):
            status = WriteStatus.CONFLICT if data.get("refuse_reason") == "conflict-membrane" else WriteStatus.REFUSED
            return GovernedWriteResult(
                accepted=False,
                refused=True,
                refuse_reason=data.get("refuse_reason"),
                refuse_detail=data.get("refuse_detail"),
                status=status,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                memory=data.get("memory"),
                lineage=data.get("lineage"),
                conflicts=data.get("conflicts", []),
            ).model_dump()
        memory = data.get("memory") or data.get("replacement") or {}
        if data.get("accepted") is not True or not memory.get("id"):
            return GovernedWriteResult(
                status=WriteStatus.UNKNOWN,
                transaction_id=transaction_id,
                correlation_id=correlation_id,
                refuse_detail="Ledger response did not prove acceptance",
                memory=data.get("memory"),
                lineage=data.get("lineage"),
            ).model_dump()
        return GovernedWriteResult(
            accepted=True,
            status=WriteStatus.ACCEPTED,
            transaction_id=transaction_id,
            correlation_id=correlation_id,
            memory=memory,
            lineage=data.get("lineage") or memory.get("lineage"),
        ).model_dump()

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()
