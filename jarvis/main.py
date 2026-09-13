"""Jarvis — main FastAPI application entry point."""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from jarvis.core.config import settings
from jarvis.persistence import AuditLedger
from jarvis.routes.chat import engine
from jarvis.routes.chat import router as chat_router
from jarvis.routes.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for Jarvis."""
    yield
    # Clean up the spiral client connection on shutdown.
    await engine.spiral.close()


app = FastAPI(
    title="Jarvis",
    version="0.1.0",
    description=(
        "Jarvis — a conversational AI assistant powered by Spiral Intelligence. "
        "Features spiral state evolution, emotion reasoning, adaptive memory, "
        "and seamless integration with the Spiral Intelligence V8 backend."
    ),
    lifespan=lifespan,
)
_security_audit = AuditLedger(settings.memory_db_path)
_rate: dict[str, list[float]] = {}
_MAX_BODY = 1_048_576
_RATE_LIMIT = 60


@app.middleware("http")
async def service_boundary(request: Request, call_next):
    """Protect state-changing and diagnostic routes when deployed with a token."""
    protected = request.url.path == "/chat" or request.url.path.startswith(("/sessions/", "/memory/"))
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    if protected:
        length = request.headers.get("content-length")
        if length and int(length) > _MAX_BODY:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [t for t in _rate.get(key, []) if now - t < 60]
        if len(recent) >= _RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
        recent.append(now)
        _rate[key] = recent
    expected = settings.service_token
    if protected and (expected or settings.environment.lower() in {"production", "prod"}):
        supplied = request.headers.get("X-Jarvis-Service-Token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            _security_audit.append(
                uuid4().hex, "security", "auth_failure", {"path": request.url.path, "request_id": request_id}
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized service request", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# CORS — origins are driven by JARVIS_CORS_ORIGINS (comma-separated).
# Default "*" is for local development only; set an explicit allowlist in production.
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
_allow_credentials = _cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(health_router)
app.mount("/ui", StaticFiles(directory="jarvis/ui", html=True), name="ui")


@app.get("/")
async def root() -> dict[str, object]:
    """Human-friendly entrypoint for local wrapper demos."""
    return {
        "service": "jarvis",
        "status": "ok",
        "message": "Jarvis wrapper is running.",
        "endpoints": {
            "health": "/health",
            "spiral_health": "/health/spiral",
            "chat": "/chat",
            "docs": "/docs",
        },
    }


@app.get("/health/ready")
async def readiness() -> dict[str, object]:
    spiral = await engine.spiral.health()
    return {
        "status": "ready" if spiral.get("status") == "ok" else "degraded",
        "service": "jarvis",
        "dependencies": {"spiral_backend": spiral},
    }
