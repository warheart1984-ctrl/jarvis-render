"""Deployment checks that should hold before public invitations. Not a security audit."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jarvis.brain.llm import LLMResult
from jarvis.core.config import JarvisSettings
from jarvis.governance.secrets import omit_secrets
from tests.test_chat_voice import client  # noqa: F401


def test_production_cors_does_not_serve_wildcard() -> None:
    config = JarvisSettings(
        _env_file=None,
        environment="production",
        service_token="deployment-token",
        cors_origins="*",
        public_origin="https://jarvis.example",
        auth_mode="operator",
    )
    assert config.allowed_cors_origins() == ["https://jarvis.example"]
    config.validate_deployment()


def test_production_cors_rejects_star_inside_allowlist() -> None:
    config = JarvisSettings(
        _env_file=None,
        environment="production",
        service_token="deployment-token",
        cors_origins="https://app.example, *",
        public_origin="https://jarvis.example",
        auth_mode="operator",
    )
    with pytest.raises(RuntimeError, match="must not include"):
        config.allowed_cors_origins()


def test_production_write_gate_stays_off_even_when_flag_is_true() -> None:
    config = JarvisSettings(
        _env_file=None,
        environment="production",
        service_token="deployment-token",
        governed_writes_enabled=True,
        cors_origins="https://jarvis.example",
        public_origin="https://jarvis.example",
        auth_mode="operator",
    )
    assert config.governed_writes_allowed() is False


def test_omit_secrets_drops_token_and_key_fields() -> None:
    public = omit_secrets(
        {
            "reply": "ok",
            "api_key": "nvapi-secret",
            "cookie": "session=abc",
            "nested": {"authorization": "Bearer secret", "model": "primary"},
        }
    )
    blob = json.dumps(public)
    assert "nvapi-secret" not in blob
    assert "Bearer secret" not in blob
    assert public["nested"]["model"] == "primary"


def test_service_token_is_not_copied_into_public_audit(client, monkeypatch):  # noqa: F811
    client, engine = client
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("Hello", "test", "text-model", 1)),
    )
    data = client.post("/chat", headers=headers, json={"user_id": "u", "message": "hi"}).json()
    audit = json.dumps(engine.get_audit(data["session_id"]))
    trace = json.dumps(engine.get_trace(data["session_id"]))
    assert "test-service-token" not in audit
    assert "test-service-token" not in trace
    assert "test-service-token" not in json.dumps(data)


def test_capabilities_do_not_echo_recall_owner_without_operator_token(client):  # noqa: F811
    client, _engine = client
    assert client.get("/capabilities").status_code == 401
    caps = client.get("/capabilities", headers={"X-Jarvis-Service-Token": "test-service-token"}).json()
    assert caps["recall_auth_mode"] == "single_operator_service_token"
    assert caps["auth_mode"] == "operator"
