"""Jarvis configuration — loaded from environment variables or .env file."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class ProviderSlot(BaseModel):
    """Server-controlled endpoint with a secret reference, never an embedded key."""

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: str
    api_key_env: str = Field(default="", pattern=r"^[A-Z0-9_]*$")
    attempts: int = Field(default=1, ge=1, le=2)
    timeout_seconds: float = Field(default=15, ge=1, le=30)
    cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    model_config = {"extra": "forbid"}

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
        ):
            raise ValueError("Use HTTPS, or HTTP on loopback for a local model; do not embed credentials.")
        return value.rstrip("/")


class JarvisSettings(BaseSettings):
    """All settings for the Jarvis service."""

    # Spiral Intelligence backend connection
    spiral_api_base: str = "http://127.0.0.1:8000"
    spiral_ws_base: str = "ws://127.0.0.1:8000"
    spiral_api_key_id: str = ""
    spiral_api_key_secret: str = ""

    # Spiral Private API (Node.js server)
    spiral_private_api_base: str = "http://127.0.0.1:8787"
    infinity_api_base: str = ""
    infinity_enabled: bool = False

    # Jarvis identity
    jarvis_user_id: str = "jarvis"
    jarvis_default_session_prefix: str = "jarvis-session"

    # OpenAI-compatible LLM provider (NVIDIA hosted API, OpenRouter, etc.)
    llm_provider: str = "mock"
    llm_api_key: str = ""
    nvidia_api_key: str = Field(default="", validation_alias="NVIDIA_API_KEY")
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    llm_temperature: float = 0.7
    llm_timeout_seconds: float = Field(default=45.0, ge=1, le=50)
    llm_attempt_timeout_seconds: float = Field(default=15.0, ge=1, le=30)
    llm_slots: list[ProviderSlot] = Field(default_factory=list, max_length=3)
    llm_fallback_models: str = "openai/gpt-oss-20b,z-ai/glm-5.3-flash"
    llm_max_tokens: int = Field(default=768, ge=64, le=4096)
    speech_asr_url: str = (
        "https://1598d209-5e27-4d3c-8079-4751568b1081.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions"
    )
    speech_tts_url: str = (
        "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio/synthesize"
    )
    speech_voice: str = "Magpie-Multilingual.EN-US.Aria"

    # Server
    host: str = "0.0.0.0"
    port: int = 8100
    cors_origins: str = "*"

    # Behaviour tuning
    max_conversation_history: int = 50
    max_long_term_memory: int = 100
    default_energy: float = 0.5
    confidence_target: float = 0.85
    care_weight: float = 0.75
    memory_db_path: str = "jarvis.sqlite3"
    continuity_ledger_url: str = ""
    continuity_ledger_token: str = ""
    service_token: str = ""
    # Single-operator recall; never derive this principal from the request's user_id.
    recall_owner_user_id: str = ""
    environment: str = "development"
    governed_writes_enabled: bool = False

    def governed_writes_allowed(self) -> bool:
        # Production promotion requires EMR gates, which are not implemented yet.
        return self.governed_writes_enabled and self.environment.lower() not in {"production", "prod"}

    def validate_deployment(self) -> None:
        if self.environment.lower() in {"production", "prod"} and not self.service_token:
            raise RuntimeError("JARVIS_SERVICE_TOKEN is required in production")

    model_config = {"env_prefix": "JARVIS_", "env_file": ".env", "extra": "ignore"}


settings = JarvisSettings()
settings.validate_deployment()
