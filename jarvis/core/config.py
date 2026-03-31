"""Jarvis configuration — loaded from environment variables or .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class JarvisSettings(BaseSettings):
    """All settings for the Jarvis service."""

    # Spiral Intelligence backend connection
    spiral_api_base: str = "http://127.0.0.1:8000"
    spiral_ws_base: str = "ws://127.0.0.1:8000"
    spiral_api_key_id: str = ""
    spiral_api_key_secret: str = ""

    # Spiral Private API (Node.js server)
    spiral_private_api_base: str = "http://127.0.0.1:8787"

    # Jarvis identity
    jarvis_user_id: str = "jarvis"
    jarvis_default_session_prefix: str = "jarvis-session"

    # LLM provider (for future integration)
    llm_provider: str = "mock"
    llm_api_key: str = ""
    llm_model: str = "gpt-4"
    llm_temperature: float = 0.7

    # Server
    host: str = "0.0.0.0"
    port: int = 8100

    # Behaviour tuning
    max_conversation_history: int = 50
    max_long_term_memory: int = 100
    default_energy: float = 0.5
    confidence_target: float = 0.85
    care_weight: float = 0.75

    model_config = {"env_prefix": "JARVIS_", "env_file": ".env", "extra": "ignore"}


settings = JarvisSettings()
