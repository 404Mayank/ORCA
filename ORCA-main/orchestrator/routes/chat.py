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

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.session import SESSIONS
from orchestrator.turn import run_turn

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
    language: str = Field(default="en", description="Phase one is English only.")
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
    llm_provider: str = "none"
    used_fallback_plan: bool = False
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)

    recommendation: dict[str, Any] | None = None


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Answer one question.

    Never returns 5xx for a domain failure. A refusal, a clarification and an
    internal failure are all 200 with a ``state`` the client renders --
    because "the venue wifi died" and "I need to know which boat" are both
    things a fisherman should see as sentences, not as an error page.
    """
    result = run_turn(
        request.query, session_id=request.session_id, language=request.language
    )
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
        llm_provider=result.llm_provider,
        used_fallback_plan=result.used_fallback_plan,
        duration_ms=result.duration_ms,
        notes=result.notes,
        recommendation=(
            result.recommendation.model_dump(mode="json")
            if (request.include_recommendation and result.recommendation is not None)
            else None
        ),
    )


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
