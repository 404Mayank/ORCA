"""GET / POST /settings -- the knobs the UI's settings sheet may turn.

Only real knobs live here. Every field is backed by something the process
actually honours:

* ``tier`` -- the model tier (free | fast | paid). POST sets a runtime
  override that :func:`orchestrator.llm.client.tier` reads on the next call,
  so the next question already uses it. No restart. Clearing the override
  (``{"tier": null}``) returns authority to ``ORCA_TIER`` in .env, which is
  still the value a fresh process boots with.

Anything the process cannot honour (theme, language) stays client-side in
localStorage and never touches this router -- a settings control that POSTs
into the void is exactly the kind of fiction CLAUDE.md forbids.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from agents.narrate import set_template_fallback, template_fallback_enabled
from orchestrator import collaborate
from orchestrator import session as session_state
from orchestrator.executor import default_timeout_s, set_default_timeout_s
from orchestrator.llm import client as llm

router = APIRouter(tags=["settings"])


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str = Field(description="Effective tier: the override when set, else ORCA_TIER.")
    tier_source: str = Field(description="'app', 'env', or 'default' (neither set).")
    tiers: list[str] = Field(description="Accepted values for POST /settings.")
    provider_order: list[str] = Field(description="Provider order the active tier uses.")
    providers: dict[str, bool] = Field(description="Which providers look usable right now.")
    deliberating: bool = Field(
        description="Agent LLM fan-out on/off. Off runs the rule floor alone: faster, fewer follow-up checks."
    )
    context_ttl_min: float = Field(description="How long a turn stays inheritable (minutes).")
    pending_ttl_min: float = Field(description="How long a clarification stays answerable (minutes).")
    max_rounds: int = Field(description="Collaboration review passes after the first execution.")
    max_added_steps: int = Field(description="Ceiling on agent-requested steps beyond the plan.")
    template_fallback: bool = Field(
        description="Fall back to the deterministic renderer when narration fails."
    )
    llm_timeout_s: float = Field(description="Per-call ceiling for LLM providers (seconds).")
    step_timeout_s: float = Field(description="Default ceiling for one tool call (seconds).")


#: Knobs that POST /settings/reset can return to compiled defaults.
#: Tier is excluded: null-clearing it already returns to ORCA_TIER.
ResettableKnob = Literal[
    "deliberating",
    "context_ttl_min",
    "pending_ttl_min",
    "max_rounds",
    "max_added_steps",
    "template_fallback",
    "llm_timeout_s",
    "step_timeout_s",
]


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["free", "fast", "paid"] | None = Field(
        default=None,
        description="Set the runtime override, or null to return authority to ORCA_TIER.",
    )
    deliberating: bool | None = Field(
        default=None,
        description="Set the deliberation override. Omitted leaves it unchanged.",
    )
    context_ttl_min: float | None = Field(default=None, ge=5, le=480)
    pending_ttl_min: float | None = Field(default=None, ge=1, le=120)
    max_rounds: int | None = Field(default=None, ge=0, le=4)
    max_added_steps: int | None = Field(default=None, ge=0, le=8)
    template_fallback: bool | None = Field(default=None)
    llm_timeout_s: float | None = Field(default=None, ge=5, le=120)
    step_timeout_s: float | None = Field(default=None, ge=5, le=120)
    reset: list[ResettableKnob] = Field(
        default_factory=list,
        description="Knobs to return to compiled defaults. Unknown names 422 via the Literal.",
    )


_RESETTABLE = {
    "deliberating": lambda: collaborate.set_deliberating(None),
    "context_ttl_min": lambda: session_state.set_context_ttl_minutes(None),
    "pending_ttl_min": lambda: session_state.set_pending_ttl_minutes(None),
    "max_rounds": lambda: collaborate.set_max_rounds(None),
    "max_added_steps": lambda: collaborate.set_max_added_steps(None),
    "template_fallback": lambda: set_template_fallback(None),
    "llm_timeout_s": lambda: llm.set_llm_timeout_s(None),
    "step_timeout_s": lambda: set_default_timeout_s(None),
}


def current_settings() -> SettingsResponse:
    """The shape both verbs return, built from live process state."""
    return SettingsResponse(
        tier=llm.tier(),
        tier_source=llm.tier_source(),
        tiers=list(llm._TIERS),
        provider_order=llm.effective_order(),
        providers=llm.provider_status(),
        deliberating=collaborate.deliberating_enabled(),
        context_ttl_min=session_state.context_ttl_minutes(),
        pending_ttl_min=session_state.pending_ttl_minutes(),
        max_rounds=collaborate.max_rounds(),
        max_added_steps=collaborate.max_added_steps(),
        template_fallback=template_fallback_enabled(),
        llm_timeout_s=llm.llm_timeout_s(),
        step_timeout_s=default_timeout_s(),
    )


@router.get("/settings", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
    """Read the knobs. Cheap, no I/O beyond key presence checks."""
    return current_settings()


@router.post("/settings", response_model=SettingsResponse)
def update_settings(update: SettingsUpdate) -> SettingsResponse:
    """Turn a knob. Unknown tiers are a 422, decided by the Literal."""
    llm.set_tier(update.tier)
    if update.deliberating is not None:
        collaborate.set_deliberating(update.deliberating)
    if update.context_ttl_min is not None:
        session_state.set_context_ttl_minutes(update.context_ttl_min)
    if update.pending_ttl_min is not None:
        session_state.set_pending_ttl_minutes(update.pending_ttl_min)
    if update.max_rounds is not None:
        collaborate.set_max_rounds(update.max_rounds)
    if update.max_added_steps is not None:
        collaborate.set_max_added_steps(update.max_added_steps)
    if update.template_fallback is not None:
        set_template_fallback(update.template_fallback)
    if update.llm_timeout_s is not None:
        llm.set_llm_timeout_s(update.llm_timeout_s)
    if update.step_timeout_s is not None:
        set_default_timeout_s(update.step_timeout_s)
    for name in update.reset:
        _RESETTABLE[name]()
    return current_settings()
