"""Authenticated audio endpoints; only persisted, audited Jarvis replies can be spoken."""

import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jarvis.brain import speech
from jarvis.brain.llm import ProviderError
from jarvis.routes.chat import engine

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/transcribe")
async def transcribe(request: Request) -> dict[str, str]:
    try:
        text = await speech.transcribe(await request.body())
        return {"text": text, "provider": "nvidia", "model": "parakeet-ctc-1.1b-asr"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


class SpeakRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)


@router.post("/speak")
async def speak(request: SpeakRequest) -> Response:
    turn = next((t for t in engine.get_trace(request.session_id) if t["turn_id"] == request.turn_id), None)
    if not turn:
        raise HTTPException(status_code=404, detail="Reply not found in this session.")
    event = next((e for e in engine.get_audit(request.session_id) if e["turn_id"] == request.turn_id), None)
    if (
        not event
        or not engine.verify_audit(request.session_id)["valid"]
        or engine.store.content_hash(turn["content"]) != turn["content_sha256"]
        or json.loads(event["payload_json"]).get("content_sha256") != turn["content_sha256"]
    ):
        raise HTTPException(status_code=409, detail="Reply audit could not be verified.")
    if len(turn["content"]) > 6000:
        raise HTTPException(status_code=400, detail="Reply is too long for speech. Please request a shorter answer.")
    try:
        audio = await speech.synthesize(turn["content"])
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
