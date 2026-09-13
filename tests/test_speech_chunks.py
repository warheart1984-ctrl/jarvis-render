import asyncio
import io
import json
import wave
from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis.brain import speech
from jarvis.brain.engine import JarvisEngine
from jarvis.brain.llm import LLMResult, ProviderError
from tests.test_chat_voice import client as client
from tests.test_chat_voice import wav_bytes


def test_chunking_preserves_order_and_bounds():
    text = "An ordinary sentence about the software engine. " * 70
    parts = speech.speech_chunks(text)
    assert " ".join(parts) == " ".join(text.split())
    assert len(parts) <= speech.MAX_SPEECH_CHUNKS
    assert all(0 < len(part) <= speech.SPEECH_CHUNK_CHARS for part in parts)
    assert "".join(speech.speech_chunks("a" * 800)) == "a" * 800


@pytest.mark.parametrize("text", ["", " \n ", "a" * 6001], ids=["empty", "blank", "oversized"])
async def test_invalid_input_makes_no_provider_request(monkeypatch, text):
    post = AsyncMock()
    monkeypatch.setattr(speech, "_post", post)
    with pytest.raises(ProviderError):
        await speech.synthesize(text)
    post.assert_not_awaited()


async def test_chunks_are_audited_before_requests_and_merged_without_wav_headers(monkeypatch):
    events, sent = [], []

    async def post(url, **kwargs):
        assert events[-1]["phase"] == "started"
        sent.append(kwargs["files"]["text"][1])
        return httpx.Response(200, content=wav_bytes(44100))

    monkeypatch.setattr(speech, "_post", post)
    text = "A longer answer about Jarvis memory and its software engine. " * 14
    audio = await speech.synthesize(text, audit_attempt=events.append)
    assert len(sent) > 1 and " ".join(sent) == text.strip()
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes() == 44100 * len(sent)
        assert len(wav.readframes(wav.getnframes())) == len(sent) * 88200
    assert len(audio) == 44 + len(sent) * 88200
    assert len(events) == len(sent) * 2 + 1
    assert events[-1]["phase"] == "assembled"
    assert "A longer answer" not in json.dumps(events)


async def test_failed_chunk_returns_no_partial_audio_and_does_not_retry(monkeypatch):
    events = []
    post = AsyncMock(side_effect=[httpx.Response(200, content=wav_bytes(44100)), ProviderError("Unavailable")])
    monkeypatch.setattr(speech, "_post", post)
    with pytest.raises(ProviderError, match="Unavailable"):
        await speech.synthesize("Long answer. " * 80, audit_attempt=events.append)
    assert post.await_count == 2
    assert events[-1]["status"] == "unavailable"
    assert all(e["phase"] != "assembled" for e in events)


async def test_total_budget_is_bounded_and_audited(monkeypatch):
    async def post(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(speech, "_post", post)
    monkeypatch.setattr(speech, "SPEECH_TOTAL_TIMEOUT", 0.01)
    events = []
    with pytest.raises(ProviderError, match="timed out"):
        await speech.synthesize("Hello", audit_attempt=events.append)
    assert events[-1]["status"] == "unknown"


async def test_audit_failure_prevents_speech_call(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(speech, "_post", post)

    def unavailable_audit(entry):
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await speech.synthesize("Hello", audit_attempt=unavailable_audit)
    post.assert_not_awaited()


@pytest.mark.parametrize(
    "content",
    [b"RIFFbad", wav_bytes(16000), wav_bytes(44100)[:-2], b"{}"],
    ids=["bad-header", "wrong-rate", "truncated", "json-body"],
)
async def test_invalid_or_mismatched_audio_cannot_be_assembled(monkeypatch, content):
    monkeypatch.setattr(speech, "_post", AsyncMock(return_value=httpx.Response(200, content=content)))
    with pytest.raises(ProviderError):
        await speech.synthesize("Hello")


async def test_combined_audio_is_bounded(monkeypatch):
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 100000)
    monkeypatch.setattr(speech, "_post", AsyncMock(return_value=httpx.Response(200, content=wav_bytes(44100))))
    with pytest.raises(ProviderError, match="Combined speech"):
        await speech.synthesize("Long answer. " * 50)


def test_audio_limit_error_is_classified_without_exposing_provider_body():
    response = httpx.Response(
        400, json={"detail": "CLIENT: Received message larger than max (4792808 vs. 4194304) SECRET"}
    )
    error = speech._speech_error(response)
    assert "audio limit" in str(error) and "SECRET" not in str(error)


def test_speech_audit_preserves_checkpoint_recovery(request, monkeypatch):
    http, engine = request.getfixturevalue("client")
    monkeypatch.setattr(
        "jarvis.brain.engine.generate_llm_reply",
        AsyncMock(return_value=LLMResult("Hello. " * 100, "nvidia", "test", 1)),
    )
    monkeypatch.setattr(speech, "_post", AsyncMock(return_value=httpx.Response(200, content=wav_bytes(44100))))
    headers = {"X-Jarvis-Service-Token": "test-service-token"}
    data = http.post("/chat", headers=headers, json={"user_id": "u", "message": "Hello"}).json()
    response = http.post(
        "/voice/speak", headers=headers, json={"session_id": data["session_id"], "turn_id": data["turn_id"]}
    )
    assert response.status_code == 200
    assert engine.verify_audit(data["session_id"])["valid"]
    events = [
        json.loads(e["payload_json"])
        for e in engine.get_audit(data["session_id"])
        if e["event_type"] == "speech_attempt"
    ]
    assert len(events) > 3
    assert all(e["transaction_id"] == data["turn_id"] for e in events)
    assert all(e["correlation_id"] == data["correlation_id"] for e in events)
    restarted = JarvisEngine(store=engine.store)
    checkpoint = restarted.reviver.recover(data["session_id"], restarted.audit)
    assert checkpoint is not None and checkpoint["state"]["turn_count"] == 1
    # Tampering with speech events still invalidates the whole chain.
    import sqlite3

    with sqlite3.connect(engine.store.path) as db:
        db.execute("UPDATE audit_events SET payload_json='{}' WHERE event_type='speech_attempt'")
    assert restarted.reviver.recover(data["session_id"], restarted.audit) is None
