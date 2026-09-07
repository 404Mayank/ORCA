"""Explainer-only retrieval over Supabase PostgREST. No new dependencies.

Reads the `rag_docs` table (db/migrations/001_rag.sql) with full-text search
over httpx, which is already a dependency. Disabled -- returning [] with no
network call -- unless SUPABASE_URL and a key are set, in which case answers
are worded with retrieved background and are otherwise identical.

THE BARRIER (reviewed, load-bearing): every passage passes through
strip_numbers() HERE, in code, before any caller sees it. Retrieval feeds
wording only: hypothesis statements, caveat phrasing, threshold explanations.
It must never populate claims[], drivers[], evidence digests, or anything the
verifier walks. A passage about "2.5 m" arrives as "about  m" -- ugly, and
that is the intended trade.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from agents.deliberate import strip_numbers

__all__ = ["enabled", "retrieve", "background_for_causal"]

_TABLE = "rag_docs"
_SNIPPET_CHARS = 600


def _credentials() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )
    if not url or not key:
        return None
    return url, key


def enabled() -> bool:
    """Whether retrieval is configured. No network is touched to answer."""
    return _credentials() is not None


def retrieve(
    query: str,
    limit: int = 3,
    _post: Callable[..., Any] | None = None,
) -> list[str]:
    """Background passages for a question, numbers stripped, or [].

    Fail-closed: any transport, shape, or server problem returns [], never
    raises. `_post` is the seam tests use instead of the network.
    """
    creds = _credentials()
    if not creds or not query.strip():
        return []
    url, key = creds
    post = _post or httpx.post
    try:
        response = post(
            f"{url}/rest/v1/{_TABLE}",
            params={"select": "source,title,body", "body": f"wfts.{query}", "limit": str(limit)},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except Exception:  # noqa: BLE001 -- fail closed, the answer proceeds without notes
        return []
    if response.status_code != 200:
        return []
    try:
        rows = response.json()
    except ValueError:
        return []
    notes: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        body = strip_numbers(str(row.get("body") or ""))[:_SNIPPET_CHARS].strip()
        if not title or not body:
            continue
        notes.append(f"{title} -- {body}")
    return notes[:limit]


def background_for_causal(place: str | None) -> list[str]:
    """Retrieval query for a causal_explain turn. [] when disabled."""
    question = f"why fish catch declined {place or ''} chlorophyll fronts".strip()
    return retrieve(question)
