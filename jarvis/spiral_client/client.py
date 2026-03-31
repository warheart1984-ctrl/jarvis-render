"""Typed async client for the Spiral Intelligence V8 backend."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from jarvis.core.config import settings
from jarvis.models.spiral_types import (
    BiofeedbackState,
    IntentMode,
    MemoryReadRequest,
    MemoryWriteRequest,
    SessionCreateRequest,
    SessionExecuteRequest,
    SessionTransitionRequest,
    SpiralCoreState,
)

logger = logging.getLogger(__name__)


class SpiralClient:
    """Async client for communicating with the Spiral Intelligence V8 backend.

    If the Spiral backend is unavailable, all methods return graceful fallbacks
    so Jarvis can still operate in standalone mode.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 15.0) -> None:
        self._base_url = (base_url or settings.spiral_api_base).rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        try:
            client = await self._get_client()
            resp = await client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Spiral backend health check failed: %s", exc)
            return {"status": "unreachable", "error": str(exc)}

    async def is_available(self) -> bool:
        result = await self.health()
        return result.get("status") == "ok"

    # ------------------------------------------------------------------
    # V8 Sessions
    # ------------------------------------------------------------------

    async def create_session(
        self,
        user_id: str,
        session_id: str,
        prompt: str,
        energy: float = 0.5,
        intent: IntentMode = IntentMode.TRANSFORM,
        biofeedback: BiofeedbackState | None = None,
        spiral_core: SpiralCoreState | None = None,
    ) -> dict[str, Any]:
        request = SessionCreateRequest(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            energy=energy,
            intent=intent,
            biofeedback=biofeedback or BiofeedbackState(),
            spiral_core=spiral_core or SpiralCoreState(),
        )
        return await self._post("/v8/session/create", request.model_dump())

    async def execute_session(
        self,
        user_id: str,
        session_id: str,
        prompt: str,
        energy: float,
        intent: IntentMode = IntentMode.TRANSFORM,
        biofeedback: BiofeedbackState | None = None,
        spiral_core: SpiralCoreState | None = None,
        dry_run: bool = True,
        section_name: str = "jarvis_section",
    ) -> dict[str, Any]:
        request = SessionExecuteRequest(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            energy=energy,
            intent=intent,
            biofeedback=biofeedback or BiofeedbackState(),
            spiral_core=spiral_core or SpiralCoreState(),
            dry_run=dry_run,
            section_name=section_name,
        )
        return await self._post("/v8/session/execute", request.model_dump())

    async def transition_session(
        self,
        user_id: str,
        session_id: str,
        to_state: str,
        reason: str = "",
    ) -> dict[str, Any]:
        request = SessionTransitionRequest(
            user_id=user_id,
            session_id=session_id,
            to_state=to_state,
            reason=reason,
        )
        return await self._post("/v8/session/transition", request.model_dump())

    async def get_session_state(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/v8/session/{session_id}/state")

    async def get_session_events(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/v8/session/{session_id}/events")

    # ------------------------------------------------------------------
    # V7 Memory
    # ------------------------------------------------------------------

    async def write_memory(
        self,
        user_id: str,
        session_id: str,
        label: str,
        energy: float,
        intent: IntentMode,
        score: float,
        notes: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = MemoryWriteRequest(
            user_id=user_id,
            session_id=session_id,
            label=label,
            energy=energy,
            intent=intent,
            score=score,
            notes=notes,
            payload=payload or {},
        )
        return await self._post("/v7/memory/write", request.model_dump())

    async def read_memory(
        self,
        user_id: str,
        session_id: str | None = None,
        intent: IntentMode | None = None,
        energy: float | None = None,
    ) -> dict[str, Any]:
        request = MemoryReadRequest(
            user_id=user_id,
            session_id=session_id,
            intent=intent,
            energy=energy,
        )
        return await self._post("/v7/memory/read", request.model_dump())

    # ------------------------------------------------------------------
    # V1 Chat (Spiral Intelligence V1 backend)
    # ------------------------------------------------------------------

    async def spiral_chat(self, user_id: str, message: str, session_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"user_id": user_id, "message": message}
        if session_id:
            payload["session_id"] = session_id
        return await self._post("/chat", payload)

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            client = await self._get_client()
            resp = await client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Spiral POST %s failed: %s", path, exc)
            return {"status": "error", "error": str(exc)}

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            client = await self._get_client()
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Spiral GET %s failed: %s", path, exc)
            return {"status": "error", "error": str(exc)}
