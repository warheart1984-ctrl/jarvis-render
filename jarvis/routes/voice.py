"""Authenticated audio endpoints; only persisted, audited Jarvis replies can be spoken."""

import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jarvis.auth import guard_visitor_mutation
from jarvis.brain import speech
from jarvis.brain.llm import ProviderError
from jarvis.core.config import settings
from jarvis.routes.chat import engine, owned_engine

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/transcribe")
async def transcribe(request: Request) -> dict[str, str]:
    principal = guard_visitor_mutation(request)
    if principal and not engine.access.consume_quota(
        "voice", principal.user_id, settings.visitor_voice_daily_limit, 86400
    ):
        raise HTTPException(status_code=429, detail="Daily voice limit reached")
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
async def speak(request: SpeakRequest, http_request: Request) -> Response:
    guard_visitor_mutation(http_request)
    active = owned_engine(http_request, request.session_id)
    turn = next((t for t in active.get_trace(request.session_id) if t["turn_id"] == request.turn_id), None)
    if not turn:
        raise HTTPException(status_code=404, detail="Reply not found in this session.")
    event = next(
        (
            e
            for e in active.get_audit(request.session_id)
            if e["turn_id"] == request.turn_id and e["event_type"] == "spiral_turn"
        ),
        None,
    )
    if (
        not event
        or not active.verify_audit(request.session_id)["valid"]
        or active.store.content_hash(turn["content"]) != turn["content_sha256"]
        or json.loads(event["payload_json"]).get("content_sha256") != turn["content_sha256"]
    ):
        raise HTTPException(status_code=409, detail="Reply audit could not be verified.")
    if len(turn["content"]) > speech.MAX_SPEECH_TEXT:
        raise HTTPException(status_code=400, detail="Reply is too long for speech. Please request a shorter answer.")
    if json.loads(event["payload_json"]).get("safe_mode"):
        raise HTTPException(status_code=409, detail="Safe-mode replies are text-only.")

    def audit_attempt(entry: dict) -> None:
        active.audit.append(
            uuid4().hex,
            request.session_id,
            "speech_attempt",
            {
                **entry,
                "transaction_id": request.turn_id,
                "correlation_id": json.loads(event["payload_json"]).get("correlation_id", request.turn_id),
                "speech_request_id": http_request.state.request_id,
            },
            turn_id=request.turn_id,
        )

    try:
        audio = await speech.synthesize(turn["content"], audit_attempt=audit_attempt)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
