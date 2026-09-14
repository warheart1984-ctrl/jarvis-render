"""Jarvis configuration — loaded from environment variables or .env file."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings

MAX_INFERENCE_SLOTS = 8


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
    # Legacy JARVIS_LLM_SLOTS override. Catalog Nemotron + Muse Glimmer fallbacks need more than 3.
    llm_slots: list[ProviderSlot] = Field(default_factory=list, max_length=MAX_INFERENCE_SLOTS)
    # Operator-ordered fallbacks after JARVIS_LLM_MODEL. NVIDIA Nemotron + Muse Glimmer
    # catalog IDs are appended in code when NVIDIA_API_KEY is set; they are not a
    # separate authority path.
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
    auth_mode: Literal["operator", "oauth"] = "operator"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_authorize_url: str = ""
    oidc_token_url: str = ""
    oidc_connection: str = "google-oauth2"
    oidc_scopes: str = "openid profile email memory.read memory.write"
    recall_signing_key: str = ""
    public_origin: str = "http://127.0.0.1:8100"
    visitor_session_hours: int = Field(default=24, ge=1, le=168)
    visitor_chat_daily_limit: int = Field(default=100, ge=1, le=10000)
    visitor_voice_daily_limit: int = Field(default=60, ge=1, le=10000)

    # Observe-only web search. Empty provider/key degrades; it never fail-closes governance.
    search_provider: str = ""
    search_api_key: str = ""
    search_base_url: str = ""
    search_timeout_seconds: float = Field(default=8.0, ge=1, le=30)
    search_attempts: int = Field(default=2, ge=1, le=2)
    search_max_results: int = Field(default=5, ge=1, le=5)
    search_max_excerpt_chars: int = Field(default=240, ge=40, le=240)
    search_max_bytes: int = Field(default=2048, ge=256, le=16384)
    search_max_response_bytes: int = Field(default=65536, ge=1024, le=262144)
    search_allow_hosts: str = ""
    search_deny_hosts: str = "localhost,127.0.0.1,::1,0.0.0.0"
    search_rate_limit: int = Field(default=8, ge=1, le=60)
    search_rate_window_seconds: int = Field(default=60, ge=10, le=3600)

    @field_validator("search_base_url")
    @classmethod
    def validate_search_endpoint(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        parsed = urlsplit(cleaned)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
        ):
            raise ValueError("Search base URL must be HTTPS, or HTTP on loopback; do not embed credentials.")
        return cleaned.rstrip("/")

    def governed_writes_allowed(self) -> bool:
        # Production cannot enable Continuity/ledger writes via the env flag.
        # Hypothesized/tool/inferred memory admission is a separate durable lock.
        return self.governed_writes_enabled and self.environment.lower() not in {"production", "prod"}

    def oauth_enabled(self) -> bool:
        return self.auth_mode == "oauth"

    def oauth_configured(self) -> bool:
        return bool(
            self.oidc_issuer.strip()
            and self.oidc_audience.strip()
            and self.oidc_jwks_url.strip()
            and self.oidc_client_id.strip()
            and self.oidc_client_secret.strip()
        )

    def identity_issuer(self) -> str:
        issuer = self.oidc_issuer.strip().rstrip("/")
        return issuer or "https://accounts.google.com"

    def identity_issuers(self) -> tuple[str, ...]:
        issuer = self.identity_issuer()
        return (issuer, issuer + "/")

    def authorize_endpoint(self) -> str:
        return self.oidc_authorize_url.strip() or f"{self.identity_issuer()}/authorize"

    def token_endpoint(self) -> str:
        return self.oidc_token_url.strip() or f"{self.identity_issuer()}/oauth/token"

    def callback_url(self) -> str:
        return self.public_origin.rstrip("/") + "/auth/callback"

    def allowed_cors_origins(self) -> list[str]:
        """Production never serves a wildcard CORS allowlist."""

        origins = [item.strip().rstrip("/") for item in self.cors_origins.split(",") if item.strip()]
        production = self.environment.lower() in {"production", "prod"}
        if not production:
            return origins or ["*"]
        if origins == ["*"] or not origins:
            origin = self.public_origin.strip().rstrip("/")
            if not origin:
                raise RuntimeError("JARVIS_CORS_ORIGINS must be an explicit allowlist in production")
            return [origin]
        if "*" in origins:
            raise RuntimeError("JARVIS_CORS_ORIGINS must not include * in production")
        return origins

    def validate_deployment(self) -> None:
        if self.environment.lower() in {"production", "prod"} and not self.service_token:
            raise RuntimeError("JARVIS_SERVICE_TOKEN is required in production")
        if self.environment.lower() in {"production", "prod"}:
            self.allowed_cors_origins()
        if self.auth_mode == "oauth":
            if not self.oauth_configured():
                raise RuntimeError(
                    "OAuth mode requires JARVIS_OIDC_ISSUER, JARVIS_OIDC_AUDIENCE, "
                    "JARVIS_OIDC_JWKS_URL, JARVIS_OIDC_CLIENT_ID, and JARVIS_OIDC_CLIENT_SECRET"
                )
            if not self.recall_signing_key:
                raise RuntimeError("JARVIS_RECALL_SIGNING_KEY is required in OAuth mode")
            parsed = urlsplit(self.public_origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise RuntimeError("JARVIS_PUBLIC_ORIGIN must be an absolute http(s) origin")
            if self.environment.lower() in {"production", "prod"} and parsed.scheme != "https":
                raise RuntimeError("JARVIS_PUBLIC_ORIGIN must be HTTPS in production")

    model_config = {"env_prefix": "JARVIS_", "env_file": ".env", "extra": "ignore"}


settings = JarvisSettings()
settings.validate_deployment()
