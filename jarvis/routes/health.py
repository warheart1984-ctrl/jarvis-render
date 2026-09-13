"""Health and diagnostics routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from jarvis.routes.chat import engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Basic health check for Jarvis."""
    return {"status": "ok", "service": "jarvis"}


@router.get("/health/spiral")
async def spiral_health() -> dict[str, Any]:
    """Check connectivity to the Spiral Intelligence backend."""
    result = await engine.spiral.health()
    return {
        "jarvis": "ok",
        "spiral_backend": result,
    }
