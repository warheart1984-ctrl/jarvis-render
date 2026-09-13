"""Continuity Ledger client contract (Phase 1 stubs only).

Durable governed memory lives in a separate ``persistence-memory``
service over HTTP/MCP. This module defines the operation surface later
phases implement. It does **not** open SQLite or treat local Jarvis
memory lists as Continuity authority.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jarvis.governance.schemas import (
    ContinuityConflict,
    ContinuityHealth,
    ContinuityMemoryRecord,
    ContinuityProposalAck,
    ContinuityRecallQuery,
    ContinuityRecallResult,
    MemoryProposal,
)


@runtime_checkable
class ContinuityLedgerClient(Protocol):
    """Phase 3 will implement these operations against persistence-memory."""

    async def recall(self, query: ContinuityRecallQuery) -> ContinuityRecallResult: ...

    async def propose_memory(self, proposal: MemoryProposal) -> ContinuityProposalAck: ...

    async def retrieve(self, memory_id: str) -> ContinuityMemoryRecord | None: ...

    async def list_conflicts(self, session_id: str) -> list[ContinuityConflict]: ...

    async def health(self) -> ContinuityHealth: ...


class UnimplementedContinuityClient:
    """Phase 1 stand-in: no HTTP client and no local SQLite authority."""

    async def recall(self, query: ContinuityRecallQuery) -> ContinuityRecallResult:
        return ContinuityRecallResult()

    async def propose_memory(self, proposal: MemoryProposal) -> ContinuityProposalAck:
        return ContinuityProposalAck(accepted=False, status="unimplemented")

    async def retrieve(self, memory_id: str) -> ContinuityMemoryRecord | None:
        return None

    async def list_conflicts(self, session_id: str) -> list[ContinuityConflict]:
        return []

    async def health(self) -> ContinuityHealth:
        return ContinuityHealth(status="unimplemented", service="persistence-memory")
