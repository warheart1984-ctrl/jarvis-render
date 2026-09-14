"""Jarvis — main FastAPI application entry point."""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from jarvis.auth import cookie_name
from jarvis.brain.llm import configured_models, configured_slots, inference_configured, provider_config
from jarvis.core.config import settings
from jarvis.persistence import AuditLedger
from jarvis.routes.auth import router as auth_router
from jarvis.routes.chat import engine
from jarvis.routes.chat import router as chat_router
from jarvis.routes.health import router as health_router
from jarvis.routes.voice import router as voice_router


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
        "Jarvis — conversational FastAPI layer with a v0 keyword emotion "
        "classifier, a bounded five-variable spiral-state tracker, adaptive "
        "memory, and an optional Spiral Intelligence V8 client. Heuristic "
        "state, not trained emotion inference or computational spiral geometry."
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
    # Match the router's actual path, never a URL reconstructed from the Host header.
    path = request.scope["path"]
    protected = path in {"/chat", "/capabilities"} or path.startswith(
        ("/sessions/", "/memory/", "/state/", "/voice/", "/governance/")
    )
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    request.state.request_id = request_id
    if protected:
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        if length > _MAX_BODY or length < 0:
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
    supplied = request.headers.get("X-Jarvis-Service-Token", "")
    request.state.operator = bool(expected) and secrets.compare_digest(supplied, expected)
    request.state.principal = engine.access.authenticate(request.cookies.get(cookie_name()) or "")
    if protected and settings.oauth_enabled():
        if not request.state.operator and request.state.principal is None:
            _security_audit.append(uuid4().hex, "security", "auth_failure", {"path": path, "request_id": request_id})
            return JSONResponse(
                status_code=401,
                content={"detail": "Sign in required", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
    elif protected and (expected or settings.environment.lower() in {"production", "prod"}):
        if not request.state.operator:
            _security_audit.append(uuid4().hex, "security", "auth_failure", {"path": path, "request_id": request_id})
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized service request", "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
    if protected and request.method in {"POST", "PUT", "PATCH"}:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if protected:
        response.headers["Cache-Control"] = "no-store"
    if path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
        )
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

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(health_router)
app.include_router(voice_router)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "ui", html=True), name="ui")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/ui/", status_code=307)


@app.get("/capabilities")
async def capabilities(request: Request) -> dict[str, object]:
    provider, key = provider_config()
    principal = None if getattr(request.state, "operator", False) else getattr(request.state, "principal", None)
    operator = getattr(request.state, "operator", False)
    recall_configured = bool(
        (principal and settings.recall_signing_key) or (settings.service_token and settings.recall_owner_user_id)
    )
    return {
        "provider": provider,
        "model": configured_models()[0] if configured_models() else settings.llm_model,
        "fallback_models": configured_models()[1:],
        "slots": [
            {
                "provider": s.provider,
                "model": s.model,
                "attempts": s.attempts,
                "timeout_seconds": s.timeout_seconds,
                "cooldown_seconds": s.cooldown_seconds,
            }
            for s in configured_slots()
        ],
        "safe_mode_available": True,
        "chat_configured": inference_configured(),
        "speech_configured": bool(settings.nvidia_api_key),
        "speech_provider": "nvidia",
        "voice_transport": "turn_based",
        "full_duplex": False,
        "auth_mode": settings.auth_mode,
        "oauth_login": settings.oauth_enabled(),
        "recall_configured": recall_configured,
        "recall_owner_user_id": settings.recall_owner_user_id if operator and settings.service_token else "",
        "recall_auth_mode": "oidc_access_token" if principal else "single_operator_service_token",
        "memory_inspection_available": True,
        "governed_writes_enabled": settings.governed_writes_allowed(),
        "new_memory_status": "draft",
        "build": "jarvis-chat-voice-v5",
    }


@app.get("/health/ready")
async def readiness() -> JSONResponse:
    # Render probes local runtime/storage, without inference calls or external quotas.
    try:
        with sqlite3.connect(engine.store.path, timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("SELECT 1 FROM spiral_turns LIMIT 1")
            db.rollback()
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"status": "not_ready", "storage": "unavailable"})
    provider, key = provider_config()
    configured = inference_configured()
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "storage": "ok",
            "provider": provider,
            "provider_configured": configured,
            "safe_mode_available": True,
            "degraded": not configured,
            "provider_connectivity": "checked_on_request",
            "build": "jarvis-chat-voice-v5",
        },
    )
