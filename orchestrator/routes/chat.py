"""POST /chat -- the endpoint the frontend will talk to.

Thin on purpose. All the reasoning lives in ``orchestrator/turn.py`` so the
HTTP path and the CLI scripts run the same code; this module only translates
between JSON and that function.

**The response carries the audit trail, not just the sentence.** A safety
answer whose evidence is not inspectable is a safety answer nobody can check,
and the explainability drawer in the UI is built from ``evidence`` and
``reasoning_trace`` rather than from a second, prettier explanation generated
after the fact.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.progress import ProgressBus
from orchestrator.routes.replay import replay_in_progress
from orchestrator.session import SESSIONS
from orchestrator.turn import TurnResult, _next_turn_id, run_turn

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(
        default=None,
        description=(
            "Opaque client-chosen id. When supplied, a follow-up inherits the "
            "previous turn's place and vessel class -- and every inherited "
            "slot is listed in assumptions, never applied silently."
        ),
    )
    language: str = Field(
        default="en",
        description=(
            "Answer prose is English; any other tag falls back to English "
            "with a recorded note (Slice 1: Tamil chrome only)."
        ),
    )
    include_recommendation: bool = Field(
        default=True,
        description="Include the full typed object. Set False for a thin client.",
    )


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    state: str = Field(description="answer | clarification | refusal | error")
    answer: str

    verdict: str | None = None
    missing_slots: list[str] = Field(
        default_factory=list,
        description="Set on a clarification: which slots the answer needs before it can be given.",
    )
    options: list[str] = Field(default_factory=list, description="Suggested answers for the client to render as choices.")
    verified: bool | None = None
    numbers_checked: int = 0
    degraded: bool = False

    collaboration: list[str] = Field(
        default_factory=list,
        description=(
            "Requests one agent made to another during this turn, e.g. the "
            "ocean agent asking the geospatial agent to check the boundary at "
            "a candidate fishing zone. Empty when no agent needed anything."
        ),
    )
    collaboration_rounds: int = 0
    agent_reasoning: list[str] = Field(
        default_factory=list,
        description=(
            "What each domain agent concluded about its own results, in its own "
            "words. Empty when no LLM was reachable -- the agents then fall back "
            "to their rule-based review, and the turn proceeds."
        ),
    )

    narration_source: str = Field(
        default="", description="'llm' when the model's prose passed the number guard, else 'template'."
    )
    suggestion_source: str = Field(
        default="",
        description="'model' | 'rules' | 'static' | 'mixed': where the answer-turn follow-ups came from. 'mixed' means the final button set was drawn from more than one source.",
    )
    llm_provider: str = "none"
    used_fallback_plan: bool = False
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)

    recommendation: dict[str, Any] | None = None


def _refuse_during_replay() -> None:
    """503 while a replay owns the process-global cache.

    A replayed 2024 storm served as current conditions would be the exact
    cache-poisoning mistake PROGRESS.md records -- so live turns fail
    loudly with a retry hint instead of answering from the wrong ocean.
    Single-worker deployments only; with --workers N each process replays
    alone (documented in routes/replay.py).
    """
    if replay_in_progress():
        raise HTTPException(
            status_code=503,
            detail="a replay is running; live answers resume when it finishes",
            headers={"Retry-After": "60"},
        )


def _to_response(result: Any, include_recommendation: bool) -> ChatResponse:
    """One translation from TurnResult to ChatResponse, shared by the
    blocking and streaming routes so they cannot drift apart."""
    return ChatResponse(
        turn_id=result.turn_id,
        state=result.state,
        answer=result.answer,
        verdict=result.verdict,
        missing_slots=result.missing_slots,
        options=result.options,
        verified=result.verified,
        numbers_checked=result.numbers_checked,
        degraded=result.degraded,
        collaboration=result.collaboration,
        collaboration_rounds=result.collaboration_rounds,
        agent_reasoning=result.agent_reasoning,
        narration_source=result.narration_source,
        suggestion_source=getattr(result, "suggestion_source", ""),
        llm_provider=result.llm_provider,
        used_fallback_plan=result.used_fallback_plan,
        duration_ms=result.duration_ms,
        notes=result.notes,
        recommendation=(
            result.recommendation.model_dump(mode="json")
            if (include_recommendation and result.recommendation is not None)
            else None
        ),
    )


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Answer one question.

    Never returns 5xx for a domain failure. A refusal, a clarification and an
    internal failure are all 200 with a ``state`` the client renders --
    because "the venue wifi died" and "I need to know which boat" are both
    things a fisherman should see as sentences, not as an error page.
    """
    _refuse_during_replay()
    result = run_turn(
        request.query, session_id=request.session_id, language=request.language
    )
    return _to_response(result, request.include_recommendation)


@router.post("/chat/stream")
def chat_stream(request: ChatRequest):
    """Same turn, streamed. Stage events while it runs, full answer at the end.

    Runs the identical ``run_turn()`` in a worker thread; the blocking
    ``POST /chat`` stays untouched as the fallback and the CLI path. Every
    ``event:`` frame comes from a real pipeline boundary (see
    ``orchestrator/progress.py``); the terminal ``done:`` frame carries the
    complete ``ChatResponse``. A client that loses the stream mid-flight
    retries with blocking ``POST /chat`` -- no partial answer is ever shown.
    """
    _refuse_during_replay()
    bus = ProgressBus()
    box: dict[str, object] = {}

    def work() -> None:
        try:
            box["result"] = run_turn(
                request.query,
                session_id=request.session_id,
                language=request.language,
                progress=bus,
            )
        except Exception as exc:  # noqa: BLE001 -- run_turn never raises, belt and braces
            # A real TurnResult, not a hand-shaped dict: the done frame must
            # validate as ChatResponse exactly like the success path.
            box["result"] = TurnResult(
                state="error",
                answer=f"{type(exc).__name__}: {exc}",
                turn_id=_next_turn_id(),
            )
        finally:
            bus.close()

    thread = threading.Thread(target=work, name="orca-stream", daemon=True)
    thread.start()

    def frames():
        while thread.is_alive():
            for event in bus.drain(timeout=1.0):
                if event is not None:
                    yield f"event: stage\ndata: {json.dumps(event)}\n\n"
        for event in bus.drain(timeout=1.0):
            if event is not None:
                yield f"event: stage\ndata: {json.dumps(event)}\n\n"
        thread.join(timeout=10.0)
        result = box.get("result")
        if result is None:  # pragma: no cover -- work() always sets one branch
            result = TurnResult(state="error", answer="stream failed", turn_id=_next_turn_id())
        payload = _to_response(result, request.include_recommendation).model_dump(mode="json")
        yield f"event: done\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream")


@router.get("/session/{session_id}")
def session_history(session_id: str) -> dict[str, Any]:
    """What this session has asked. For the UI's history pane and for debugging."""
    turns = SESSIONS.history(session_id)
    return {
        "session_id": session_id,
        "turns": [
            {
                "turn_id": t.turn_id,
                "query": t.query,
                "state": t.state,
                "verdict": t.verdict,
                "created_at": t.created_at.isoformat(),
                "age_minutes": round(t.age_minutes, 1),
                "inheritable": t.inheritable,
            }
            for t in turns
        ],
    }


@router.delete("/session/{session_id}")
def forget_session(session_id: str) -> dict[str, Any]:
    """Drop a session's context, so the next question starts clean."""
    return {"session_id": session_id, "forgotten": SESSIONS.forget(session_id)}
