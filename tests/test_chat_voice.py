from __future__ import annotations

import io
import json
import sqlite3
import wave
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.brain import llm, speech
from jarvis.brain.engine import JarvisEngine
from jarvis.core.config import settings
from jarvis.models.jarvis_types import ChatRequest
from jarvis.persistence import JarvisStore


def wav_bytes(rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * rate)
    return output.getvalue()


@pytest.fixture
def nvidia(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test-only")


def mock_provider(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_nvidia_sends_valid_history_and_bounded_request(monkeypatch, nvidia):
    def handler(req):
        data = json.loads(req.content)
        assert req.url.host == "integrate.api.nvidia.com"
        assert data["max_tokens"] == 768
        assert all(set(m) == {"role", "content"} for m in data["messages"])
        assert data["chat_template_kwargs"]["enable_thinking"] is False
        assert req.headers["authorization"] == "Bearer nvapi-test-only"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello there"}}]})

    mock_provider(monkeypatch, handler)
    result = await llm.generate_llm_reply([{"role": "user", "content": "Hi", "timestamp": "ignored"}])
    assert result.reply == "Hello there"
    assert result.provider == "nvidia" and result.latency_ms >= 0
    assert not result.cost_reported


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [(401, {}), (410, {}), (429, {}), (500, {}), (200, {}), (200, {"choices": [{"message": {"content": None}}]})],
)
async def test_provider_failure_is_not_success(monkeypatch, nvidia, status, body):
    mock_provider(monkeypatch, lambda req: httpx.Response(status, json=body))
    with pytest.raises(llm.ProviderError) as error:
        await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert "nvapi-test-only" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_is_sanitized(monkeypatch, nvidia):
    def handler(req):
        raise httpx.ReadTimeout("private provider detail nvapi-test-only", request=req)

    mock_provider(monkeypatch, handler)
    with pytest.raises(llm.ProviderError, match="timed out") as error:
        await llm.generate_llm_reply([{"role": "user", "content": "Hello"}])
    assert "nvapi" not in str(error.value)


@pytest.mark.asyncio
async def test_read_only_conversation_reaches_llm_without_external_writes(monkeypatch, tmp_path):
    from jarvis.models.jarvis_types import EmotionState

    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(confidence_bias=0))
    response = llm.LLMResult("Hello, how can I help?", "nvidia", "test", 17)
    generate = AsyncMock(return_value=response)
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    engine = JarvisEngine(store=JarvisStore(tmp_path / "chat.sqlite3"))
    sync = AsyncMock()
    monkeypatch.setattr(engine, "_sync_with_spiral", sync)
    result = await engine.chat(ChatRequest(user_id="owner", message="Remember my name", memory_consent=True))
    assert result.reply == response.reply
    assert result.decision == "fail_closed" and result.read_only
    assert result.uncertainty == 0.5
    assert result.memory_snapshot["long_term_entries"] == 0
    generate.assert_awaited_once()
    sync.assert_not_awaited()
    saved = engine.get_trace(result.session_id)[0]
    assert saved["provider"] == "nvidia" and saved["latency_ms"] == 17
    assert engine.verify_audit(result.session_id)["valid"]


@pytest.mark.asyncio
async def test_failed_llm_does_not_advance_history(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply", AsyncMock(side_effect=llm.ProviderError("Unavailable"))
    )
    engine = JarvisEngine(store=JarvisStore(tmp_path / "fail.sqlite3"))
    state = await engine.get_or_create_session("owner")
    with pytest.raises(llm.ProviderError):
        await engine.chat(ChatRequest(user_id="owner", session_id=state.session_id, message="Hello"))
    assert engine.get_session(state.session_id).turn_count == 0
    assert not engine.get_trace(state.session_id)


@pytest.mark.asyncio
async def test_high_stress_blocks_llm(monkeypatch, tmp_path):
    from jarvis.models.jarvis_types import EmotionState

    generate = AsyncMock()
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", generate)
    monkeypatch.setattr("jarvis.brain.engine.infer_emotion", lambda *a: EmotionState(stress=0.9))
    engine = JarvisEngine(store=JarvisStore(tmp_path / "stress.sqlite3"))
    result = await engine.chat(ChatRequest(user_id="u", message="hello"))
    assert result.decision == "fail_closed" and result.provider == "local"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_session_ownership(monkeypatch, tmp_path):
    engine = JarvisEngine(store=JarvisStore(tmp_path / "recover.sqlite3"))
    result = await engine.chat(ChatRequest(user_id="owner", message="hello"))
    restarted = JarvisEngine(store=engine.store)
    with pytest.raises(ValueError, match="does not belong"):
        await restarted.get_or_create_session("intruder", result.session_id)
    assert restarted.get_session(result.session_id) is None


@pytest.mark.asyncio
async def test_speech_contracts(monkeypatch, nvidia):
    def handler(req):
        assert req.headers["authorization"] == "Bearer nvapi-test-only"
        assert "multipart/form-data" in req.headers["content-type"]
        if req.url.path.endswith("transcriptions"):
            assert b'name="file"' in req.content and b"RIFF" in req.content
            return httpx.Response(200, json={"text": "Hello Jarvis"})
        assert b"Magpie-Multilingual.EN-US.Aria" in req.content
        return httpx.Response(200, content=wav_bytes(44100), headers={"content-type": "audio/wav"})

    mock_provider(monkeypatch, handler)
    assert await speech.transcribe(wav_bytes()) == "Hello Jarvis"
    assert (await speech.synthesize("Hello")).startswith(b"RIFF")
    with pytest.raises(ValueError):
        await speech.transcribe(wav_bytes(44100))


@pytest.fixture
def client(monkeypatch, tmp_path):
    import jarvis.main as main
    import jarvis.routes.chat as chat
    import jarvis.routes.voice as voice

    engine = JarvisEngine(store=JarvisStore(tmp_path / "api.sqlite3"))
    for mod in [main, chat, voice]:
        monkeypatch.setattr(mod, "engine", engine)
    monkeypatch.setattr(settings, "service_token", "test-service-token")
    monkeypatch.setattr(settings, "environment", "production")
    main._rate.clear()
    with TestClient(main.app) as client:
        yield client, engine


def test_auth_voice_and_audited_reply(client, monkeypatch):
    client, engine = client
    for path in ["/chat", "/voice/transcribe", "/voice/speak", "/sessions/resume"]:
        assert client.post(path, content=b"").status_code == 401
    for path in ["/state/anything", "/capabilities"]:
        assert client.get(path).status_code == 401
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=llm.LLMResult("Audited hello", "nvidia", "test", 20)),
    )
    monkeypatch.setattr(speech, "synthesize", AsyncMock(return_value=wav_bytes(44100)))
    response = client.post("/chat", headers=headers, json={"user_id": "u", "message": "hi", "input_mode": "voice"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["input_mode"] == "voice"
    reply_ref = {"session_id": data["session_id"], "turn_id": data["turn_id"]}
    spoken = client.post("/voice/speak", headers=headers, json=reply_ref)
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    assert client.post("/voice/speak", headers=headers, json={**reply_ref, "turn_id": "wrong"}).status_code == 404
    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE spiral_turns SET content='tampered'")
    assert client.post("/voice/speak", headers=headers, json=reply_ref).status_code == 409
    assert client.post("/voice/transcribe", headers=headers, content=b"x" * 1_048_577).status_code == 413
    assert client.post("/voice/transcribe", headers=headers, content=b"bad").status_code == 400
    assert client.get("/").url.path == "/ui/"
    assert client.get("/health/ready").status_code == 200


def test_api_provider_failure_and_resume(client, monkeypatch):
    client, engine = client
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply", AsyncMock(side_effect=llm.ProviderError("Unavailable"))
    )
    assert client.post("/chat", headers=headers, json={"user_id": "u", "message": "hi"}).status_code == 503
    monkeypatch.setattr("jarvis.brain.engine.generate_llm_reply", AsyncMock(return_value=None))
    data = client.post("/chat", headers=headers, json={"user_id": "u", "message": "hello"}).json()
    import jarvis.routes.chat as chat

    monkeypatch.setattr(chat, "engine", JarvisEngine(store=engine.store))
    response = client.post("/sessions/resume", headers=headers, json={"user_id": "u", "session_id": data["session_id"]})
    assert response.json()["state"]["read_only"]
    assert (
        client.post(
            "/chat", headers=headers, json={"user_id": "u", "session_id": data["session_id"], "message": "continue"}
        ).status_code
        == 400
    )
