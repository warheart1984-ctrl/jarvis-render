"""NVIDIA hosted speech adapters. Audio stays in memory and keys stay server-side."""

from __future__ import annotations

import io
import wave
from urllib.parse import urlparse

import httpx

from jarvis.brain.llm import ProviderError
from jarvis.core.config import settings


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
            raise ProviderError(f"NVIDIA speech returned HTTP {response.status_code}.")
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


async def synthesize(text: str) -> bytes:
    response = await _post(
        settings.speech_tts_url,
        files={
            "text": (None, text),
            "language": (None, "en-US"),
            "voice": (None, settings.speech_voice),
            "encoding": (None, "LINEAR_PCM"),
            "sample_rate_hz": (None, "44100"),
        },
    )
    audio = response.content
    if not audio or len(audio) > 20_000_000:
        raise ProviderError("NVIDIA speech returned invalid audio.")
    # Some Riva revisions return raw LINEAR_PCM; provide a browser-playable WAV.
    if audio[:4] != b"RIFF":
        if "json" in response.headers.get("content-type", "") or len(audio) % 2:
            raise ProviderError("NVIDIA speech returned invalid audio.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(audio)
        audio = output.getvalue()
    return audio
