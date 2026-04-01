"""Jarvis — main FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jarvis.core.config import settings
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
