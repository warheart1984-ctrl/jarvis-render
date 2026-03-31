"""Jarvis — main FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

# CORS — allow all origins for development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(health_router)
