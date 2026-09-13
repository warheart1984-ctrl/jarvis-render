"""NVIDIA hosted speech adapters. Audio stays in memory and keys stay server-side."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import textwrap
import time
import wave
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from jarvis.brain.llm import ProviderError
from jarvis.core.config import settings

MAX_SPEECH_TEXT = 6000
SPEECH_CHUNK_CHARS = 240
MAX_SPEECH_CHUNKS = 40
MAX_AUDIO_BYTES = 20_000_000
SPEECH_TOTAL_TIMEOUT = 50


def _speech_error(response: httpx.Response) -> ProviderError:
    # Classify known provider failures without forwarding its body or echoed text.
    detail = response.text[:4096].lower() if response.status_code == 400 else ""
    if "received message larger than max" in detail:
        message = "Generated speech exceeded the provider audio limit. Please request a shorter reply."
    elif "maximum input length" in detail or "maximum allowed length" in detail or "2000" in detail:
        message = "Speech text exceeded the provider normalized-text limit. Please request a shorter reply."
    elif "voice" in detail and ("not found" in detail or "does not exist" in detail or "invalid" in detail):
        message = "The configured speech voice was rejected by the provider. Text chat remains available."
    else:
        message = f"NVIDIA speech returned HTTP {response.status_code}."
    return ProviderError(message, retryable=False)


def validate_wav(audio: bytes) -> None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError("Use mono 16-bit PCM WAV at 16000 Hz.")
            frames = wav.getnframes()
            if not 1600 <= frames <= 480000:
                raise ValueError("Record between 0.1 and 30 seconds of audio.")
            if len(wav.readframes(frames)) != frames * 2:
                raise ValueError("Incomplete WAV recording.")
    except (wave.Error, EOFError):
        raise ValueError("Invalid WAV recording.") from None


def _headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".invocation.api.nvcf.nvidia.com"):
        raise ProviderError("Speech endpoint must be a hosted NVIDIA HTTPS endpoint.")
    if not settings.nvidia_api_key:
        raise ProviderError("NVIDIA_API_KEY is not configured for speech.")
    return {"Authorization": f"Bearer {settings.nvidia_api_key}"}


async def _post(url: str, **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            response = await client.post(url, headers=_headers(url), **kwargs)
        if response.status_code != 200:
            raise _speech_error(response)
        return response
    except httpx.TimeoutException:
        raise ProviderError("NVIDIA speech timed out. Try a shorter recording.") from None
    except httpx.HTTPError:
        raise ProviderError("NVIDIA speech is unavailable.") from None


async def transcribe(audio: bytes) -> str:
    validate_wav(audio)
    response = await _post(
        settings.speech_asr_url,
        files={"file": ("recording.wav", audio, "audio/wav")},
        data={"language": "en-US", "word_time_offsets": "False"},
    )
    try:
        body = response.json()
        text = body.get("text", "")
    except (ValueError, AttributeError):
        raise ProviderError("NVIDIA speech returned an invalid transcription.") from None
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("No speech was recognized. Please try again.")
    if len(text) > 16000:
        raise ProviderError("Transcription is too long.")
    return text.strip()


def speech_chunks(text: str) -> list[str]:
    """Bound text and generated audio size; preserve words and sentence order."""
    if not text.strip() or len(text) > MAX_SPEECH_TEXT:
        raise ProviderError("Speech requires a nonempty reply of at most 6000 characters.", retryable=False)
    normalized = " ".join(text.split())
    chunks: list[str] = []
    pending = ""
    for sentence in re.split(r"(?<=[.!?])\s+", normalized):
        parts = textwrap.wrap(sentence, width=SPEECH_CHUNK_CHARS, break_on_hyphens=False)
        for part in parts:
            if pending and len(pending) + 1 + len(part) > SPEECH_CHUNK_CHARS:
                chunks.append(pending)
                pending = ""
            pending = (pending + " " + part).strip()
    if pending:
        chunks.append(pending)
    if len(chunks) > MAX_SPEECH_CHUNKS:
        raise ProviderError("Reply requires too many speech parts. Please request a shorter reply.", retryable=False)
    return chunks


def _pcm(response: httpx.Response) -> bytes:
    audio = response.content
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ProviderError("NVIDIA speech returned invalid audio.")
    if audio[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 44100):
                    raise ProviderError("NVIDIA speech returned an unexpected audio format.")
                frames = wav.getnframes()
                pcm = wav.readframes(frames)
                if not frames or len(pcm) != frames * 2:
                    raise ProviderError("NVIDIA speech returned incomplete audio.")
                return pcm
        except (wave.Error, EOFError):
            raise ProviderError("NVIDIA speech returned invalid audio.")
    # Some Riva revisions return raw LINEAR_PCM instead of a WAV container.
    mime = response.headers.get("content-type", "").split(";")[0]
    if mime not in {"audio/pcm", "audio/wav", "application/octet-stream"} or len(audio) % 2:
        raise ProviderError("NVIDIA speech returned invalid audio.")
    return audio


async def synthesize(text: str, *, audit_attempt: Callable[[dict[str, Any]], None] | None = None) -> bytes:
    chunks = speech_chunks(text)
    pcm = bytearray()

    def record(entry: dict[str, Any]) -> None:
        if audit_attempt:
            audit_attempt(entry)

    try:
        async with asyncio.timeout(SPEECH_TOTAL_TIMEOUT):
            for index, chunk in enumerate(chunks, 1):
                envelope = {
                    "provider": "nvidia",
                    "model": "magpie-tts-multilingual",
                    "chunk": index,
                    "total_chunks": len(chunks),
                    "characters": len(chunk),
                    "text_sha256": hashlib.sha256(chunk.encode()).hexdigest(),
                    "attempt": 1,
                }
                started = time.perf_counter()
                record({**envelope, "phase": "started", "status": "unknown"})
                try:
                    response = await _post(
                        settings.speech_tts_url,
                        files={
                            "text": (None, chunk),
                            "language": (None, "en-US"),
                            "voice": (None, settings.speech_voice),
                            "encoding": (None, "LINEAR_PCM"),
                            "sample_rate_hz": (None, "44100"),
                        },
                    )
                    part = _pcm(response)
                    if len(pcm) + len(part) + 44 > MAX_AUDIO_BYTES:
                        raise ProviderError("Combined speech is too long. Please request a shorter reply.")
                except (ProviderError, asyncio.CancelledError) as exc:
                    record(
                        {
                            **envelope,
                            "phase": "completed",
                            "status": "unknown" if isinstance(exc, asyncio.CancelledError) else "unavailable",
                            "reason": "cancelled_or_timed_out" if isinstance(exc, asyncio.CancelledError) else str(exc),
                            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        }
                    )
                    raise
                record(
                    {
                        **envelope,
                        "phase": "completed",
                        "status": "accepted",
                        "audio_bytes": len(part),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    }
                )
                pcm.extend(part)
    except TimeoutError:
        raise ProviderError("Speech synthesis timed out. Your complete text reply is still available.") from None
    # Never concatenate WAV headers or return partially successful speech.
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    record(
        {"phase": "assembled", "status": "accepted", "total_chunks": len(chunks), "audio_bytes": len(output.getvalue())}
    )
    return output.getvalue()
