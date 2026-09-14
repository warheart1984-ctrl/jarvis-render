import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis.brain import llm, speech
from jarvis.brain.engine import JarvisEngine
from jarvis.core.config import ProviderSlot, settings
from jarvis.models.jarvis_types import ChatRequest, EmotionState
from jarvis.persistence import JarvisStore
from tests.test_chat_voice import client, mock_provider  # noqa: F401


@pytest.fixture
def models(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_model", "primary")
    monkeypatch.setattr(settings, "llm_fallback_models", "backup,last")
    monkeypatch.setattr(llm, "NVIDIA_FALLBACK_MODELS", ())


@pytest.mark.parametrize(
    "content,finish",
    [
        ("As a helpful assistant</think>Hello", "stop"),
        ("<think>internal text</think>Hi", "stop"),
        ("Half an answer", "length"),
    ],
    ids=["orphan-think", "reasoning-block", "truncated-answer"],
)
async def test_malformed_answers_fall_back_without_exposing_content(monkeypatch, models, content, finish):
    def handler(req):
        primary = json.loads(req.content)["model"] == "primary"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": content if primary else "A clean answer."},
                        "finish_reason": finish if primary else "stop",
                    }
                ]
            },
        )

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.reply == "A clean answer." and result.model == "backup"
    assert result.attempts[0]["status"] == "unknown"
    assert content not in json.dumps(result.attempts)


@pytest.mark.parametrize("status", [410, 429, 503, 200])
async def test_fallback_records_real_model_and_failures(monkeypatch, models, status):
    def handler(req):
        model = json.loads(req.content)["model"]
        if model == "primary":
            return httpx.Response(status, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Real backup reply"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.model == "backup" and result.fallback_used
    assert [a["status"] for a in result.attempts] == ["unknown" if status == 200 else "unavailable", "accepted"]
    assert result.reply == "Real backup reply"
    assert not result.cost_reported


@pytest.mark.parametrize("status", [401, 403])
async def test_bad_key_does_not_retry_other_models(monkeypatch, models, status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, json={})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    assert len(calls) == 1


async def test_all_models_down_is_not_a_success(monkeypatch, models):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(503, json={})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    assert result.provider == "internal" and result.model == "minimal-chat"
    assert result.attempts[-1]["inference_confirmed"] is False
    assert calls == ["primary", "backup", "last"]


async def test_timeout_falls_back_and_skips_failed_model_during_cooldown(monkeypatch, models):
    calls = []
    slots = [slot.model_copy(update={"timeout_seconds": 0.01}) for slot in llm.configured_slots()]
    monkeypatch.setattr(settings, "llm_slots", slots)

    async def attempt(messages, slot, key):
        calls.append(slot.model)
        if slot.model == "primary":
            await asyncio.sleep(1)
        return llm.LLMResult("hello", slot.provider, slot.model, 1)

    monkeypatch.setattr(llm, "_request_model", attempt)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.model == "backup"
    again = await llm.generate_llm_reply([{"role": "user", "content": "Hello again"}])
    assert calls == ["primary", "backup", "backup"]
    assert again.attempts[0]["reason"] == "cooldown"


async def test_fallback_is_saved_in_audit_and_trace(monkeypatch, tmp_path):
    attempts = [{"model": "primary", "status": "unavailable"}, {"model": "backup", "status": "accepted"}]
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=llm.LLMResult("Hello", "nvidia", "backup", 18, attempts=attempts, fallback_used=True)),
    )
    engine = JarvisEngine(store=JarvisStore(tmp_path / "fallback.sqlite3"))
    response = await engine.chat(ChatRequest(user_id="u", message="hi"))
    assert response.fallback_used and response.provider_attempts == attempts
    event = json.loads(engine.get_audit(response.session_id)[0]["payload_json"])
    assert event["provider_attempts"] == attempts and event["model"] == "backup"
    trace = engine.get_trace(response.session_id)[0]
    assert trace["model"] == "backup"
    assert engine.verify_audit(response.session_id)["valid"]


async def test_simultaneous_turns_preserve_both_histories(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", AsyncMock(return_value=None))
    engine = JarvisEngine(store=JarvisStore(tmp_path / "concurrent.sqlite3"))
    state = await engine.get_or_create_session("u")
    await asyncio.gather(
        *[
            engine.chat(ChatRequest(user_id="u", session_id=state.session_id, message=message))
            for message in ["first", "second"]
        ]
    )
    assert engine.get_session(state.session_id).turn_count == 2
    assert len(engine.get_session(state.session_id).conversation_history) == 4


def test_speech_failure_does_not_disable_text_chat(client, monkeypatch):  # noqa: F811
    client, engine = client
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    monkeypatch.setattr(speech, "synthesize", AsyncMock(side_effect=llm.ProviderError("Voice unavailable")))
    monkeypatch.setattr(speech, "transcribe", AsyncMock(side_effect=llm.ProviderError("Voice unavailable")))
    data = client.post("/chat", headers=headers, json={"user_id": "u", "message": "hi"}).json()
    assert client.post("/voice/transcribe", headers=headers, content=b"audio").status_code == 503
    assert (
        client.post(
            "/voice/speak", headers=headers, json={"session_id": data["session_id"], "turn_id": data["turn_id"]}
        ).status_code
        == 503
    )
    response = client.post(
        "/chat", headers=headers, json={"user_id": "u", "message": "Still here?", "session_id": data["session_id"]}
    )
    assert response.status_code == 200
    assert engine.verify_audit(data["session_id"])["valid"]


async def test_provider_slots_isolate_credentials_and_correlate(monkeypatch):
    monkeypatch.setenv("PRIMARY_KEY", "primary-secret")
    monkeypatch.setenv("SECONDARY_KEY", "secondary-secret")
    slots = [
        ProviderSlot(provider="vendor-a", model="first", base_url="https://a.example/v1", api_key_env="PRIMARY_KEY"),
        ProviderSlot(
            provider="compatible", model="second", base_url="https://b.example/v1", api_key_env="SECONDARY_KEY"
        ),
    ]
    monkeypatch.setattr(settings, "llm_slots", slots)
    events = []

    def handler(req):
        assert events[-1]["phase"] == "started"
        if req.url.host == "a.example":
            assert req.headers["authorization"] == "Bearer primary-secret"
            return httpx.Response(401, json={})
        assert req.headers["authorization"] == "Bearer secondary-secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Second provider"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply(
        [{"role": "user", "content": "hi"}],
        transaction_id="transaction",
        correlation_id="correlation",
        audit_attempt=events.append,
    )
    assert result.provider == "compatible" and not result.safe_mode
    assert [e["phase"] for e in events] == ["started", "completed", "started", "completed"]
    assert all(e["transaction_id"] == "transaction" and e["correlation_id"] == "correlation" for e in events)
    assert "secret" not in json.dumps(result.attempts)


async def test_local_slot_receives_no_cloud_key(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "private-cloud-key")
    monkeypatch.setattr(
        settings, "llm_slots", [ProviderSlot(provider="local", model="llama.cpp", base_url="http://127.0.0.1:8080/v1")]
    )

    def handler(req):
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "Local model answer"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.provider == "local" and not result.safe_mode


async def test_content_refusal_does_not_hunt_for_another_model(monkeypatch, models):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"refusal": "Policy refusal"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "test"}])
    assert result.safe_mode and result.inference_status == "refused"
    assert len(calls) == 1


def test_safe_mode_is_availability_not_governance_fail_closed(client, monkeypatch, models):  # noqa: F811
    client, engine = client
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    monkeypatch.setattr(
        "jarvis.brain.engine.infer_emotion",
        lambda *a, **k: EmotionState(confidence_bias=0.2, inferred_emotion="driven", stress=0.1),
    )
    monkeypatch.setattr(llm, "_request_model", AsyncMock(side_effect=llm.ProviderError("Unavailable")))
    sync = AsyncMock()
    monkeypatch.setattr(engine, "_sync_with_spiral", sync)
    response = client.post(
        "/chat",
        headers=headers,
        json={"user_id": "u", "message": "Let's build a safer planner.", "memory_consent": True},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["safe_mode"] and data["decision"] == "degraded" and data["read_only"]
    assert data["inference_status"] == "unavailable"
    assert "inference" not in (data["fail_closed_reason"] or "")
    assert data["memory_snapshot"]["long_term_entries"] == 0
    sync.assert_not_awaited()
    attempts = [e for e in engine.get_audit(data["session_id"]) if e["event_type"] == "inference_attempt"]
    assert len(attempts) == 7  # Start/outcome for three providers, then the minimal status response.
    assert all(json.loads(e["payload_json"])["transaction_id"] == data["turn_id"] for e in attempts)
    assert (
        client.post(
            "/voice/speak", headers=headers, json={"session_id": data["session_id"], "turn_id": data["turn_id"]}
        ).status_code
        == 409
    )
    assert engine.verify_audit(data["session_id"])["valid"]
    again = client.post(
        "/chat", headers=headers, json={"user_id": "u", "session_id": data["session_id"], "message": "Still here?"}
    ).json()
    assert again["safe_mode"] and again["decision"] == "degraded"
    assert again["memory_snapshot"]["conversation_turns"] == 2


async def test_overall_budget_stops_retries(monkeypatch):
    slot = ProviderSlot(provider="local", model="test", base_url="http://127.0.0.1:8080/v1", attempts=2)
    slot = slot.model_copy(update={"timeout_seconds": 0.05})
    monkeypatch.setattr(settings, "llm_slots", [slot, slot])
    monkeypatch.setattr(settings, "llm_timeout_seconds", 0.01)
    calls = []

    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(1)

    monkeypatch.setattr(llm, "_request_model", slow)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.safe_mode and len(calls) == 1


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://user:pass@example.com/v1", "file:///tmp/model"])
def test_slot_endpoint_restrictions(url):
    with pytest.raises(ValueError):
        ProviderSlot(provider="test", model="test", base_url=url)


async def test_missing_keys_use_safe_mode_without_network(monkeypatch, models):
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    attempt = AsyncMock()
    monkeypatch.setattr(llm, "_request_model", attempt)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    attempt.assert_not_awaited()


async def test_cannot_call_provider_without_auditing_start(monkeypatch, models):
    attempt = AsyncMock()
    monkeypatch.setattr(llm, "_request_model", attempt)

    def broken_audit(entry):
        raise RuntimeError("Storage unavailable")

    with pytest.raises(RuntimeError, match="Storage unavailable"):
        await llm.generate_llm_reply([{"role": "user", "content": "hi"}], audit_attempt=broken_audit)
    attempt.assert_not_awaited()


async def test_slot_retry_count_is_bounded(monkeypatch):
    monkeypatch.setattr(
        settings,
        "llm_slots",
        [ProviderSlot(provider="local", model="test", base_url="http://127.0.0.1:8080/v1", attempts=2)],
    )
    attempt = AsyncMock(side_effect=llm.ProviderError("Unavailable"))
    monkeypatch.setattr(llm, "_request_model", attempt)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.safe_mode and attempt.await_count == 2


def test_readiness_reports_safe_mode_without_provider_key(client, monkeypatch, models):  # noqa: F811
    client, engine = client
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["degraded"]
    assert not response.json()["provider_configured"]


def test_nvidia_catalog_is_appended_after_existing_fallbacks_when_key_is_set(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(settings, "llm_fallback_models", "openai/gpt-oss-20b,z-ai/glm-5.3-flash")
    models = llm.configured_models()
    assert models[:3] == [
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "openai/gpt-oss-20b",
        "z-ai/glm-5.3-flash",
    ]
    assert models[3:] == [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "nvidia/nemotron-3-super-120b-a12b",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "meta/muse-glimmer-30b",
    ]
    assert all(slot.provider == "nvidia" for slot in llm.configured_slots())
    assert all(slot.api_key_env == "NVIDIA_API_KEY" for slot in llm.configured_slots())


def test_missing_nvidia_key_skips_catalog_fallbacks(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(settings, "llm_fallback_models", "openai/gpt-oss-20b,z-ai/glm-5.3-flash")
    assert llm.configured_models() == [
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "openai/gpt-oss-20b",
        "z-ai/glm-5.3-flash",
    ]
    assert "meta/muse-glimmer-30b" not in llm.configured_models()


async def test_missing_nvidia_key_does_not_call_catalog_models(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(settings, "llm_fallback_models", "openai/gpt-oss-20b")
    attempt = AsyncMock()
    monkeypatch.setattr(llm, "_request_model", attempt)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    attempt.assert_not_awaited()
    dumped = json.dumps(result.attempts)
    assert "muse-glimmer" not in dumped
    assert "nemotron-3-super" not in dumped
    assert "credential_unavailable" in dumped


async def test_nvidia_catalog_is_tried_after_prior_provider_failure(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "llm_api_key", "openrouter-secret")
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test-only")
    monkeypatch.setattr(settings, "llm_model", "primary-open")
    monkeypatch.setattr(settings, "llm_base_url", "https://openrouter.example/v1")
    monkeypatch.setattr(llm, "NVIDIA_FALLBACK_MODELS", ("nvidia/nemotron-3-super-120b-a12b", "meta/muse-glimmer-30b"))
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append((req.url.host, model, req.headers.get("authorization")))
        if model == "primary-open":
            return httpx.Response(503, json={})
        if model == "nvidia/nemotron-3-super-120b-a12b":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Muse Glimmer reply"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.model == "meta/muse-glimmer-30b" and result.provider == "nvidia" and result.fallback_used
    assert result.reply == "Muse Glimmer reply" and not result.safe_mode
    assert calls == [
        ("openrouter.example", "primary-open", "Bearer openrouter-secret"),
        ("integrate.api.nvidia.com", "nvidia/nemotron-3-super-120b-a12b", "Bearer nvapi-test-only"),
        ("integrate.api.nvidia.com", "meta/muse-glimmer-30b", "Bearer nvapi-test-only"),
    ]


async def test_nvidia_4xx_is_not_retried_forever(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test-only")
    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(settings, "llm_fallback_models", "")
    monkeypatch.setattr(llm, "NVIDIA_FALLBACK_MODELS", ("meta/muse-glimmer-30b",))
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(400, json={"error": "bad request"})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    assert result.provider == "internal" and result.model == "minimal-chat"
    assert calls == ["nvidia/nemotron-3.5-lightning-30b-a3b", "meta/muse-glimmer-30b"]


async def test_nvidia_4xx_does_not_retry_the_same_slot(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test-only")
    monkeypatch.setattr(
        settings,
        "llm_slots",
        [
            ProviderSlot(
                provider="nvidia",
                model="meta/muse-glimmer-30b",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
                attempts=2,
            )
        ],
    )
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(404, json={})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "hi"}])
    assert result.safe_mode and result.inference_status == "unavailable"
    assert calls == ["meta/muse-glimmer-30b"]


def test_explicit_slots_do_not_auto_append_nvidia_catalog(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "test-only")
    monkeypatch.setattr(
        settings,
        "llm_slots",
        [
            ProviderSlot(
                provider="nvidia",
                model="openai/gpt-oss-20b",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            )
        ],
    )
    assert llm.configured_models() == ["openai/gpt-oss-20b"]


def test_capabilities_include_nvidia_catalog_in_fallback_sequence(client, monkeypatch):  # noqa: F811
    client, _engine = client
    monkeypatch.setattr(settings, "llm_provider", "nvidia")
    monkeypatch.setattr(settings, "nvidia_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_model", "nvidia/nemotron-3.5-lightning-30b-a3b")
    monkeypatch.setattr(settings, "llm_fallback_models", "openai/gpt-oss-20b,z-ai/glm-5.3-flash")
    caps = client.get("/capabilities", headers={"X-Jarvis-Service-Token": "test-service-token"}).json()
    models = [caps["model"], *caps["fallback_models"]]
    assert models[-1] == "meta/muse-glimmer-30b"
    assert caps["slots"][-1]["provider"] == "nvidia"
    assert "nvidia/nemotron-3-ultra-550b-a55b" in models
