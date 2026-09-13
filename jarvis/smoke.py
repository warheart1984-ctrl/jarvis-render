"""Run a small deployed text/speech test using existing server environment secrets.

Execute inside Render: python -m jarvis.smoke
Only synthetic test messages and result metadata are printed. Never prints keys.
Creates test conversation records; does not alter real sessions.
"""

import io
import json
import os
import wave
from array import array

import httpx


def run():
    headers = {"X-Jarvis-Service-Token": os.environ["JARVIS_SERVICE_TOKEN"]}
    with httpx.Client(base_url="http://127.0.0.1:8100", headers=headers, timeout=65) as client:
        assert client.get("/health/ready").status_code == 200
        assert httpx.post("http://127.0.0.1:8100/chat", json={}).status_code == 401
        caps = client.get("/capabilities").json()
        assert caps["chat_configured"] and caps["speech_configured"]
        response = client.post(
            "/chat", json={"user_id": "deployment-smoke", "message": "Say hello in one short sentence."}
        )
        response.raise_for_status()
        first = response.json()
        assert first["provider"] == "nvidia" and first["reply"]
        assert first["inference_status"] == "accepted" and not first["safe_mode"]
        response = client.post(
            "/chat",
            json={
                "user_id": "deployment-smoke",
                "session_id": first["session_id"],
                "message": "What did I just ask you? Reply in one short sentence.",
            },
        )
        response.raise_for_status()
        second = response.json()
        assert second["provider"] == "nvidia"
        assert second["inference_status"] == "accepted" and not second["safe_mode"]
        response = client.post("/voice/speak", json={"session_id": first["session_id"], "turn_id": first["turn_id"]})
        response.raise_for_status()
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            rate = wav.getframerate()
            samples = array("h", wav.readframes(wav.getnframes()))
        # Smoke-only resampling of synthetic speech. Browser recording uses averaged PCM.
        downsampled = array("h", [samples[int(i * rate / 16000)] for i in range(int(len(samples) * 16000 / rate))])
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(downsampled.tobytes())
        response = client.post("/voice/transcribe", content=output.getvalue(), headers={"Content-Type": "audio/wav"})
        response.raise_for_status()
        transcription = response.json()["text"]
        response = client.post(
            "/chat",
            json={
                "user_id": "deployment-smoke",
                "session_id": first["session_id"],
                "message": transcription,
                "input_mode": "voice",
            },
        )
        response.raise_for_status()
        spoken_turn = response.json()
        assert spoken_turn["input_mode"] == "voice" and spoken_turn["provider"] == "nvidia"
        response = client.post(
            "/voice/speak", json={"session_id": first["session_id"], "turn_id": spoken_turn["turn_id"]}
        )
        response.raise_for_status()
        assert response.content[:4] == b"RIFF"
        assert client.get("/sessions/" + first["session_id"] + "/audit/verify").json()["valid"]
        print(
            json.dumps(
                {
                    "status": "passed",
                    "model": first["model"],
                    "followup_model": second["model"],
                    "fallback_used": first["fallback_used"] or second["fallback_used"],
                    "provider_attempts": first["provider_attempts"],
                    "text_reply": first["reply"],
                    "followup": second["reply"],
                    "transcription": transcription,
                    "voice_reply": spoken_turn["reply"],
                    "audio_bytes": len(response.content),
                    "session_id": first["session_id"],
                    "auth": "enforced",
                    "audit": "verified",
                }
            )
        )


if __name__ == "__main__":
    run()
