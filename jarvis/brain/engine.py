"""Jarvis engine — orchestrates conversation, v0 heuristic state, DOS-lite deliberation, memory, and replies."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, assert_never
from uuid import uuid4

from jarvis.auth import AccessStore, Principal
from jarvis.brain.context import build_chat_context
from jarvis.brain.deliberation import (
    ChallengeAction,
    DeliberationBlocked,
    DeliberationRunner,
    challenge_reply,
    evidence_from_citation,
    map_challenge_decision,
    message_looks_hypothetical,
)
from jarvis.brain.emotion import infer_emotion
from jarvis.brain.llm import ProviderError, generate_llm_reply
from jarvis.brain.memory import (
    add_long_term_memory,
    add_to_conversation_history,
    extract_memory,
    update_preferences,
)
from jarvis.brain.provenance import context_receipt, memory_reference
from jarvis.brain.responder import generate_response
from jarvis.brain.spiral_evolution import determine_phase, evolve_spiral
from jarvis.brain.tools import (
    evidence_from_search_hit,
    may_admit_retrieved_to_memory,
    maybe_web_search,
    quoted_search_payload,
    search_citation,
)
from jarvis.continuity import ContinuityLedgerClient
from jarvis.core.config import settings
from jarvis.models.jarvis_types import (
    ChatRequest,
    ChatResponse,
    JarvisState,
    SessionLockReason,
    SpiralTurn,
)
from jarvis.persistence import AuditLedger, JarvisStore
from jarvis.persistence.recall import RecallLedger, RecallResult
from jarvis.persistence.reviver import ReviverLedger
from jarvis.spiral_client import SpiralClient
from jarvis.spiral_client.infinity import ProjectInfinityClient

logger = logging.getLogger(__name__)

_LOCK_MESSAGES = {
    SessionLockReason.RECOVERY: (
        "Session is read-only after verified recovery (reason=recovery). Start a new chat to continue."
    ),
    SessionLockReason.CONFLICT: ("Session is read-only pending conflict resolution (reason=conflict)."),
    SessionLockReason.VERIFICATION: (
        "Session is read-only because audit or turn verification failed (reason=verification). Start a new chat."
    ),
}


class JarvisEngine:
    """The main Jarvis orchestrator.

    Manages sessions, runs the v0 emotion classifier, spiral-state tracker,
    and DOS-lite deliberation pipeline, and optionally syncs state with the
    Spiral Intelligence backend.
    """

    def __init__(self, spiral_client: SpiralClient | None = None, store: JarvisStore | None = None) -> None:
        self._sessions: dict[str, JarvisState] = {}
        self._global_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.spiral = spiral_client or SpiralClient()
        self.store = store or JarvisStore(settings.memory_db_path)
        self.access = AccessStore(self.store.path)
        self.audit = AuditLedger(self.store.path, *self.store.scope)
        self.reviver = ReviverLedger(self.store.path, *self.store.scope)
        self.recall = RecallLedger(self.store.path, *self.store.scope)
        self._tenant_engines: dict[tuple[str, str], JarvisEngine] = {}
        self._read_only_reasons: dict[str, SessionLockReason] = {}
        self.continuity = (
            ContinuityLedgerClient(settings.continuity_ledger_url, settings.continuity_ledger_token)
            if settings.continuity_ledger_url
            else None
        )
        self.infinity = (
            ProjectInfinityClient(settings.infinity_api_base, settings.service_token)
            if settings.infinity_enabled and settings.infinity_api_base
            else None
        )
        self.search_backend = None

    def for_principal(self, principal: Principal) -> JarvisEngine:
        scope = principal.tenant_id, principal.subject
        if scope not in self._tenant_engines:
            child = JarvisEngine(store=JarvisStore(self.store.path, *scope))
            child.infinity = None
            self._tenant_engines[scope] = child
        child = self._tenant_engines[scope]
        if settings.continuity_ledger_url and "memory.write" in principal.scopes and principal.ledger_token:
            child.continuity = ContinuityLedgerClient(settings.continuity_ledger_url, principal.ledger_token)
        else:
            child.continuity = None
        return child

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session asyncio lock, creating one if needed."""
        async with self._global_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            return self._session_locks[session_id]

    async def get_or_create_session(self, user_id: str, session_id: str | None = None) -> JarvisState:
        async with self._global_lock:
            if session_id and session_id in self._sessions:
                state = self._sessions[session_id]
                if state.user_id != user_id:
                    raise ValueError("Session does not belong to this user.")
                return state

            new_id = session_id or f"{settings.jarvis_default_session_prefix}-{uuid4().hex[:8]}"
            now = datetime.now(timezone.utc).isoformat()
            state = JarvisState(
                session_id=new_id,
                user_id=user_id,
                energy=settings.default_energy,
                created_at=now,
                updated_at=now,
            )
            if session_id:
                recovered = self.reviver.recover(new_id, self.audit)
                if recovered:
                    try:
                        state = JarvisState.model_validate(recovered["state"])
                    except Exception:
                        raise ValueError("Recovery checkpoint is invalid; start a new session.") from None
                    if state.user_id != user_id or state.session_id != new_id:
                        raise ValueError("Session does not belong to this user.")
                    self.set_read_only(new_id, SessionLockReason.RECOVERY)
                elif self.audit.list(new_id) or self.reviver.latest_verified(new_id):
                    raise ValueError("Session recovery could not be verified; start a new session.")
            self._sessions[new_id] = state
            self._session_locks[new_id] = asyncio.Lock()
            return state

    def get_session(self, session_id: str) -> JarvisState | None:
        return self._sessions.get(session_id)

    def _save_session(self, state: JarvisState) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        self._sessions[state.session_id] = state

    # ------------------------------------------------------------------
    # Main conversation loop
    # ------------------------------------------------------------------

    async def chat(
        self, request: ChatRequest, *, recall_owner: str | None = None, principal: Principal | None = None
    ) -> ChatResponse:
        """Process a user message through the six-step turn pipeline.

        Steps:
        1. LISTEN  — receive the message and resolve the session
        2. ORIENT  — v0 keyword emotion classifier + phase label
        3. REASON  — advance the bounded five-variable spiral-state tracker,
                     then run the v0 DOS-lite deliberation stages through Challenge
        4. RESPOND — generate a contextual reply; Evaluate + Commit tag claims
        5. REFLECT — extract memories and update preferences
        6. EVOLVE  — persist the turn; optional backend sync (disabled pending EMR)
        """

        # This optional principal comes from token verification in the server route,
        # never from request.context or another client-supplied field.
        recall_key = settings.service_token
        if principal is not None:
            if (
                not settings.oauth_enabled()
                or not settings.recall_signing_key
                or request.user_id != principal.user_id
                or not self.access.owns(principal, request.session_id or "")
                or self.store.scope != (principal.tenant_id, principal.subject)
            ):
                raise ValueError("Visitor ownership check failed")
            recall_owner, recall_key = principal.user_id, principal.recall_key()
        elif recall_owner is not None and (
            not settings.service_token
            or recall_owner != settings.recall_owner_user_id
            or recall_owner != request.user_id
        ):
            raise ValueError("Recall ownership check failed")
        state = await self.get_or_create_session(request.user_id, request.session_id)
        if state.session_id in self._read_only_reasons:
            reason = self._read_only_reasons[state.session_id]
            raise ValueError(_LOCK_MESSAGES[reason])
        session_lock = await self._get_session_lock(state.session_id)

        async with session_lock:
            # Work on a copy so a failed request cannot advance the live session.
            state = self._sessions[state.session_id].model_copy(deep=True)
            # --- 1. LISTEN ---
            biofeedback = request.biofeedback or state.biofeedback

            # --- 2. ORIENT ---
            emotion = infer_emotion(request.message, biofeedback)
            state.emotion = emotion
            state.biofeedback = biofeedback

            confidence = max(0.25, min(0.95, state.confidence + emotion.confidence_bias))

            phase = determine_phase(request.message, confidence, state.turn_count)
            state.phase = phase

            # --- 3. REASON ---
            new_core, new_intent, new_energy = evolve_spiral(
                core=state.spiral_core,
                intent=state.intent,
                energy=state.energy,
                emotion=emotion,
                confidence=confidence,
                turn_count=state.turn_count,
            )
            state.spiral_core = new_core
            state.intent = new_intent
            state.energy = new_energy
            state.confidence = confidence
            uncertainty = round(1.0 - confidence, 4)
            reasons = []
            if uncertainty >= 0.5:
                reasons.append("uncertainty at or above safe execution threshold")
            if emotion.stress > 0.8:
                reasons.append("stress above safe execution threshold")
            decision = "fail_closed" if reasons else "answer"

            owned_memories = [
                m for m in state.long_term_memory if m.user_id == state.user_id and m.session_id == state.session_id
            ]
            runner = DeliberationRunner()
            turn_id = uuid4().hex
            correlation_id = uuid4().hex
            # Observe-only search: explicit request only. Failure degrades; it is not fail-closed.
            search_record = await maybe_web_search(
                message=request.message,
                search_query=request.search_query,
                session_id=state.session_id,
                tenant_id=self.store.tenant_id,
                owner_sub=self.store.owner_sub,
                transaction_id=turn_id,
                correlation_id=correlation_id,
                quota=self.access.consume_quota,
                backend=self.search_backend,
            )
            if search_record is not None:
                self.audit.append(
                    uuid4().hex,
                    state.session_id,
                    "tool_call",
                    search_record.to_public_dict(),
                    turn_id=turn_id,
                )
                for hit in search_record.sources:
                    runner.add_evidence(evidence_from_search_hit(hit))
            runner.observe(
                message=request.message,
                session_id=state.session_id,
                memories=[(m.memory_id, m.content) for m in owned_memories[:8]],
                history=[
                    (
                        str(item.get("turn_id") or index),
                        f"{item.get('role', 'message')}: {item.get('content', '')}",
                    )
                    for index, item in enumerate(state.conversation_history[-8:])
                    if item.get("role") in {"user", "assistant"}
                ],
                external_context=request.context or None,
                emotion_rationale=emotion.rationale,
            )
            runner.interpret(
                emotion_label=emotion.inferred_emotion,
                intent=state.intent.value,
                phase=state.phase.value,
                confidence=confidence,
            )
            runner.infer()
            if message_looks_hypothetical(request.message):
                runner.simulate()
            runner.challenge(uncertainty=uncertainty, stress=emotion.stress)
            if runner.challenge_action is not None:
                decision = map_challenge_decision(runner.challenge_action, decision)
            for reason in runner.challenge_reasons:
                if reason not in reasons:
                    reasons.append(reason)
            if decision == "fail_closed" and not reasons:
                reasons.append("deliberation challenge fail-closed")

            # --- 4. RESPOND ---
            local_reply, reasoning_trace = generate_response(state, request.message)
            forced_reply = challenge_reply(runner.challenge_action or ChallengeAction.CONTINUE)
            if forced_reply and decision != "fail_closed":
                local_reply = forced_reply
                reasoning_trace.append(f"dos-lite challenge forced {runner.challenge_action.value}")

            def audit_attempt(entry: dict[str, Any]) -> None:
                self.audit.append(uuid4().hex, state.session_id, "inference_attempt", entry, turn_id=turn_id)

            llm_result = None
            previous = RecallResult()
            if request.recall_previous:
                previous = (
                    self.recall.previous(recall_owner, recall_key, session_id=state.session_id, before=state.created_at)
                    if recall_owner
                    else RecallResult({"status": "not_authorized"})
                )
            search_quotes = (
                quoted_search_payload(search_record.sources) if search_record and search_record.sources else None
            )
            search_citations = (
                [search_citation(item, session_id=state.session_id) for item in search_record.sources]
                if search_record
                else None
            )
            messages, runtime_context = build_chat_context(
                state,
                request,
                read_only=bool(reasons),
                infinity_configured=self.infinity is not None,
                continuity_configured=self.continuity is not None,
                speech_configured=bool(settings.nvidia_api_key),
                previous=previous,
                search_quotes=search_quotes or None,
                search_citations=search_citations or None,
                search_status=search_record.status.value if search_record else "not_requested",
            )
            if request.recall_previous:
                self.audit.append(
                    uuid4().hex,
                    state.session_id,
                    "previous_session_recall",
                    {
                        **runtime_context["previous_session"],
                        "transaction_id": turn_id,
                        "correlation_id": correlation_id,
                    },
                    turn_id=turn_id,
                )
            # Language-only clarification is allowed even when consequential actions
            # are blocked. No tools, external memory writes or execution are offered.
            skip_hosted = runner.challenge_action in {ChallengeAction.CLARIFY, ChallengeAction.ABSTAIN}
            if emotion.stress <= 0.8 and decision != "abstain" and not skip_hosted:
                try:
                    llm_result = await generate_llm_reply(
                        messages,
                        transaction_id=turn_id,
                        correlation_id=correlation_id,
                        audit_attempt=audit_attempt,
                    )
                except ProviderError as exc:
                    # A missing dependency is not an apparently successful local answer.
                    raise ProviderError(str(exc)) from None
            reply = llm_result.reply if llm_result else local_reply
            if llm_result and llm_result.safe_mode:
                # Provider unavailability is not a governance fail-closed.
                if decision != "fail_closed":
                    decision = "degraded"
            if decision == "fail_closed" and not llm_result:
                reply = (
                    "I’m pausing consequential action because the available signals are uncertain. "
                    "I can clarify the goal or continue with read-only planning."
                )
                reasoning_trace.append("fail-closed safety lane applied")
            elif decision == "abstain" and not llm_result:
                reply = forced_reply or (
                    "I'm abstaining from a committed answer. The available evidence is too thin "
                    "or the signals are too uncertain for a DOS-lite v0 commit."
                )
                reasoning_trace.append("dos-lite challenge abstain applied")
            elif decision == "degraded" and llm_result and llm_result.safe_mode:
                reasoning_trace.append("provider availability degraded; inference not confirmed")

            # --- 5. REFLECT ---
            prepared_citations = runtime_context.pop("prepared_citations")
            for item in prepared_citations:
                cited = evidence_from_citation(item)
                if cited:
                    runner.add_evidence(cited)
            receipt = context_receipt(prepared_citations, llm_result)
            runtime_context["context_receipt"] = receipt
            try:
                runner.evaluate(reply)
                if runner.gated_reply:
                    reply = runner.gated_reply
                if runner.challenge_action is not None:
                    decision = map_challenge_decision(runner.challenge_action, decision)
                if runner.response_commit == "abstained" or runner.challenge_action is ChallengeAction.ABSTAIN:
                    decision = "abstain"
                elif runner.response_commit == "refused" and decision == "answer":
                    decision = "fail_closed"
                    reasons.append("safety-critical claim lacked verification")
                deliberation = runner.commit()
            except DeliberationBlocked as exc:
                reasons.append(str(exc))
                decision = "fail_closed"
                deliberation = runner.snapshot(status="blocked", blocked_reason=str(exc))
            public_deliberation = deliberation.to_public_dict()
            for stage in deliberation.stages:
                reasoning_trace.append(f"dos-lite {stage.name.value}: {stage.summary}")
            state.conversation_history = add_to_conversation_history(state, request.message, reply)
            for message in state.conversation_history[-2:]:
                message["turn_id"] = turn_id

            snippets = [hit.excerpt for hit in search_record.sources] if search_record else []
            memory_entry = (
                extract_memory(state, request.message, reply)
                if request.memory_consent and decision == "answer" and runner.memory_admission == "eligible"
                else None
            )
            if memory_entry and snippets and not may_admit_retrieved_to_memory(
                user_requested=request.memory_consent
            ):
                if any(snippet and snippet in memory_entry.content for snippet in snippets):
                    memory_entry = None
            if memory_entry:
                state.long_term_memory = add_long_term_memory(state, memory_entry)

            if request.memory_consent and decision == "answer" and runner.memory_admission == "eligible":
                state.preferences = update_preferences(state, request.message)
            state.turn_count += 1
            turn = SpiralTurn(
                turn_id=turn_id,
                session_id=state.session_id,
                timestamp=datetime.now(timezone.utc),
                decision=decision,
                uncertainty=uncertainty,
                stress=emotion.stress,
                fail_closed_reason="; ".join(reasons) if reasons else None,
                evidence=[
                    {"type": "emotion", "rationale": emotion.rationale},
                    {"type": "channel", "input_mode": request.input_mode},
                    {"type": "provider_usage", "cost_reported": llm_result.cost_reported if llm_result else True},
                    {"type": "provider_attempts", "attempts": llm_result.attempts if llm_result else []},
                    {"type": "runtime_context", **runtime_context},
                    {"type": "deliberation", "version": "v0-dos-lite", "trace": public_deliberation},
                    {
                        "type": "tool_calls",
                        "calls": [search_record.to_public_dict()] if search_record else [],
                    },
                ],
                content=reply,
                content_sha256=hashlib.sha256(reply.encode()).hexdigest(),
            )
            # --- 6. EVOLVE (sync with Spiral backend if available) ---
            backend_status = "skipped_read_only"
            if decision == "answer" and request.memory_consent:
                backend_status = "skipped_writes_disabled"
                if settings.governed_writes_allowed():
                    backend_status = await self._sync_with_spiral(state, request.message, reply)
            turn.latency_ms = llm_result.latency_ms if llm_result else 0.0
            turn.provider = llm_result.provider if llm_result else "local"
            turn.model = llm_result.model if llm_result else "bounded-local"
            turn.cost_usd = llm_result.cost_usd if llm_result else 0.0
            turn.backend_status = backend_status
            self.store.save_turn_bundle(
                turn,
                {
                    "event_id": turn_id,
                    "event_type": "spiral_turn",
                    "timestamp": turn.timestamp.isoformat(),
                    "payload": {
                        "decision": decision,
                        "uncertainty": uncertainty,
                        "stress": emotion.stress,
                        "reason": turn.fail_closed_reason,
                        "content_sha256": turn.content_sha256,
                        "provider": turn.provider,
                        "model": turn.model,
                        "latency_ms": turn.latency_ms,
                        "input_mode": request.input_mode,
                        "memory_consent": request.memory_consent,
                        "cost_reported": llm_result.cost_reported if llm_result else True,
                        "provider_attempts": llm_result.attempts if llm_result else [],
                        "fallback_used": llm_result.fallback_used if llm_result else False,
                        "safe_mode": llm_result.safe_mode if llm_result else False,
                        "inference_status": llm_result.inference_status if llm_result else "not_requested",
                        "transaction_id": turn_id,
                        "correlation_id": correlation_id,
                        "runtime_context": runtime_context,
                        "memory_record": memory_reference(memory_entry) if memory_entry else None,
                        "deliberation": public_deliberation,
                        "tool_calls": [search_record.to_public_dict()] if search_record else [],
                    },
                },
                {
                    "id": memory_entry.memory_id,
                    "session_id": state.session_id,
                    "content": memory_entry.content,
                    "created_at": memory_entry.created_at,
                    "confidence": memory_entry.importance,
                    "type": memory_entry.category,
                    "subject": state.user_id,
                    "status": "draft",
                    "evidence": [{"source": "chat"}],
                }
                if memory_entry
                else None,
            )

            audit_event = self.audit.list(state.session_id)[-1]
            self.reviver.save(
                checkpoint_id=f"checkpoint-{turn_id}",
                session_id=state.session_id,
                turn_id=turn_id,
                audit_hash=audit_event["event_hash"],
                state=state.model_dump(mode="json"),
                verified=True,
            )
            if recall_owner:
                try:
                    self.recall.attest(
                        state.session_id,
                        recall_owner,
                        recall_key,
                        expected_state=state.model_dump(mode="json"),
                    )
                except Exception:
                    # The turn is stored, but no signed recall checkpoint was confirmed.
                    # Lock rather than continue with live state behind durable state.
                    self.set_read_only(state.session_id, SessionLockReason.VERIFICATION)
                    raise RuntimeError("Recall checkpoint could not be confirmed; session locked") from None

            self._save_session(state)

            return ChatResponse(
                session_id=state.session_id,
                reply=reply,
                intent=state.intent,
                phase=state.phase,
                energy=state.energy,
                confidence=state.confidence,
                spiral_state=state.spiral_core.model_dump(),
                emotion=state.emotion.model_dump(),
                memory_snapshot={
                    "conversation_turns": state.turn_count,
                    "long_term_entries": len(state.long_term_memory),
                    "preferences": state.preferences,
                },
                reasoning_trace=reasoning_trace,
                turn_id=turn_id,
                decision=decision,
                uncertainty=uncertainty,
                fail_closed_reason=turn.fail_closed_reason,
                provider=turn.provider,
                model=turn.model,
                cost_usd=turn.cost_usd,
                latency_ms=turn.latency_ms,
                read_only=decision in {"fail_closed", "abstain", "degraded"}
                or bool(llm_result and llm_result.safe_mode),
                cost_reported=llm_result.cost_reported if llm_result else True,
                input_mode=request.input_mode,
                provider_attempts=llm_result.attempts if llm_result else [],
                fallback_used=llm_result.fallback_used if llm_result else False,
                safe_mode=llm_result.safe_mode if llm_result else False,
                inference_status=llm_result.inference_status if llm_result else "not_requested",
                transaction_id=turn_id,
                correlation_id=correlation_id,
                previous_session=runtime_context["previous_session"],
                context_receipt=receipt,
                lock_reason=self.lock_reason(state.session_id),
                tool_calls=[search_record.to_public_dict()] if search_record else [],
                deliberation=public_deliberation,
            )

    # ------------------------------------------------------------------
    # State retrieval
    # ------------------------------------------------------------------

    def get_state_summary(self, session_id: str) -> dict[str, Any]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        lock_reason = self.lock_reason(session_id)
        return {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "phase": state.phase.value,
            "intent": state.intent.value,
            "energy": state.energy,
            "confidence": state.confidence,
            "turn_count": state.turn_count,
            "read_only": lock_reason is not None,
            "lock_reason": lock_reason.value if lock_reason else None,
            "recovered": lock_reason is SessionLockReason.RECOVERY,
            "memory_count": len(state.long_term_memory),
            "spiral_core": state.spiral_core.model_dump(),
            "emotion": state.emotion.model_dump(),
            "preferences": state.preferences,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }

    def get_memory_summary(self, session_id: str) -> dict[str, Any]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        return {
            "session_id": state.session_id,
            "conversation_history": state.conversation_history,
            "long_term_memory": [m.model_dump() for m in state.long_term_memory],
            "preferences": state.preferences,
        }

    def get_trace(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent structured, user-safe decision traces."""
        return self.store.load_session_turns(session_id, limit)

    def get_audit(self, session_id: str) -> list[dict[str, Any]]:
        return self.audit.list(session_id)

    def verify_audit(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "valid": self.audit.verify(session_id)}

    def set_read_only(self, session_id: str, reason: SessionLockReason | str = SessionLockReason.CONFLICT) -> None:
        locked = reason if isinstance(reason, SessionLockReason) else SessionLockReason(reason)
        match locked:
            case SessionLockReason.RECOVERY | SessionLockReason.CONFLICT | SessionLockReason.VERIFICATION:
                if session_id not in self._read_only_reasons:
                    self._read_only_reasons[session_id] = locked
            case _:
                assert_never(locked)

    def lock_reason(self, session_id: str) -> SessionLockReason | None:
        return self._read_only_reasons.get(session_id)

    def is_read_only(self, session_id: str) -> bool:
        return session_id in self._read_only_reasons

    def authorize_session(self, session_id: str) -> None:
        """Clear a conflict lock after a verified supersession. Recovery/verification stay locked."""

        if self._read_only_reasons.get(session_id) is SessionLockReason.CONFLICT:
            self._read_only_reasons.pop(session_id, None)

    def clear_memory(self, session_id: str) -> dict[str, str]:
        state = self.get_session(session_id)
        if state is None:
            raise ValueError("Session not found.")

        self.recall.revoke(session_id)
        state.conversation_history = []
        state.long_term_memory = []
        state.preferences = {}
        self._save_session(state)

        return {"status": "cleared", "session_id": session_id}

    # ------------------------------------------------------------------
    # Spiral backend sync
    # ------------------------------------------------------------------

    async def _sync_with_spiral(self, state: JarvisState, user_message: str, reply: str) -> str:
        """Optionally push state to the Spiral Intelligence backend."""

        if not settings.governed_writes_allowed() or self.store.tenant_id != "t_jon":
            return "skipped_writes_disabled"

        try:
            if self.infinity:
                await self.infinity.evolve(
                    job_id=f"jarvis-{state.session_id}-{state.turn_count}",
                    jarvis_run_id=state.session_id,
                    task=user_message,
                    initial_candidate=reply,
                )
            if not await self.spiral.is_available():
                return "unavailable"

            await self.spiral.spiral_turn(
                session_id=state.session_id,
                prompt=user_message,
                energy=state.energy,
                intent=state.intent,
            )

            # Push a chat turn to the V1 backend.
            await self.spiral.spiral_chat(
                user_id=state.user_id,
                message=user_message,
                session_id=state.session_id,
            )

            # Write a memory entry to the V7 backend.
            await self.spiral.write_memory(
                user_id=state.user_id,
                session_id=state.session_id,
                label=f"jarvis:{state.intent.value}:{state.turn_count}",
                energy=state.energy,
                intent=state.intent,
                score=state.confidence,
                notes=f"Jarvis turn {state.turn_count}: {user_message[:100]}",
            )
            return "connected"
        except Exception as exc:
            logger.debug("Spiral sync skipped: %s", exc)
            return "error"
