"""The FastAPI application.

    uvicorn orchestrator.main:app --reload
    http://127.0.0.1:8000/docs

Assembles the routes and nothing else. Anything that reasons belongs in
``orchestrator/turn.py``; anything that computes belongs in ``tools/``.

**Startup does not fetch.** CLAUDE.md forbids fetching during a query, and a
process that populated its cache on boot would make the first request after a
restart mysteriously slow and the venue's wifi a dependency of starting the
server. ``scripts/refresh_cache.py`` fills the cache; this reads it. The
startup banner reports what it found so an empty cache is visible immediately
rather than at the first question.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import agents  # noqa: F401 -- populates the agent registry
import core.env  # noqa: F401 -- loads .env before any config is read
import tools  # noqa: F401 -- populates the tool registry
from orchestrator.routes import chat, geo, health, replay, settings

logger = logging.getLogger("orca")

app = FastAPI(
    title="ORCA",
    version="0.1.0",
    description=(
        "Marine ecosystem reasoning with collaborative agents. "
        "SIH PS 26176. Every number in an answer is traceable to the tool "
        "call that produced it."
    ),
)

# The frontend is a separate Vite dev server, so it is cross-origin from the
# start. This used to be `allow_origins=["*"]`, with a note saying that was
# fine while the API held no credentials and no authenticated state, and that
# the line should be revisited the moment that changed.
#
# It changed: the deployed API is behind a shared-secret header
# (deploy/modal_app.py), so a wildcard would now let a page on any domain
# invite a visitor's browser to spend this deployment's tokens.
#
# So the origins come from ORCA_ALLOWED_ORIGINS, comma-separated. Unset means
# "*", which keeps local development and the CLI exactly as they were -- the
# laptop has no gate to protect and no bill to run up. It is the deployment
# that sets the variable, and there is deliberately only one CORS layer:
# stacking a second one in the Modal wrapper had two middlewares racing to
# set the same header on every response.
_origins = [o.strip() for o in os.environ.get("ORCA_ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(settings.router)
app.include_router(geo.router)
app.include_router(replay.router)


@app.on_event("startup")
def report_readiness() -> None:
    """Log what the cache holds, so an empty one is obvious at boot."""
    from ingest.sources import open_meteo

    marine = open_meteo.load_cached("marine", 10.77, 79.84)
    if marine is None:
        logger.warning(
            "ORCA started with an EMPTY weather cache. Every safety answer "
            "will fail until you run: python scripts/refresh_cache.py"
        )
    else:
        logger.info("ORCA started; weather cache from %s", marine.fetched_at.isoformat())


@app.get("/")
def index() -> dict[str, object]:
    return {
        "service": "ORCA",
        "problem_statement": "SIH 26176",
        "docs": "/docs",
        "endpoints": ["/health", "/readiness", "/chat", "/chat/stream", "/session/{id}", "/settings", "/geo/boundaries", "/replay", "/replay/{event_id}"],
    }
