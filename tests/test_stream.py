"""POST /chat/stream emits real pipeline stages, then the full answer.

The load-bearing guarantee: every ``stage`` frame maps to a boundary the
pipeline actually crossed. No percentages, no prose, no invented activity --
a progress channel that fabricates would be worse than the static line it
replaces. The narration text itself never streams (the number guard passes
on the complete text only); the terminal ``done`` frame carries the whole
``ChatResponse``, byte-comparable to the blocking route.
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

import orchestrator.turn as turn_module
from orchestrator.main import app
from orchestrator.progress import STAGES

SAFETY_QUERY = "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"


def _frames(response) -> tuple[list[dict], dict]:
    """Split an SSE response into (stage events, done payload)."""
    stages: list[dict] = []
    done: dict = {}
    for chunk in response.text.strip().split("\n\n"):
        if not chunk.strip():
            continue
        kind, _, data = chunk.partition("\ndata: ")
        payload = json.loads(data)
        if kind.strip() == "event: stage":
            stages.append(payload)
        elif kind.strip() == "event: done":
            done = payload
    return stages, done


def _stream(client: TestClient, query: str, session: str) -> tuple[list[dict], dict]:
    with client.stream(
        "POST", "/chat/stream", json={"query": query, "session_id": session}
    ) as response:
        assert response.status_code == 200
        text = response.read().decode()
    stages: list[dict] = []
    done: dict = {}
    for chunk in text.strip().split("\n\n"):
        if not chunk.strip():
            continue
        kind, _, data = chunk.partition("\ndata: ")
        payload = json.loads(data)
        if kind.strip() == "event: stage":
            stages.append(payload)
        elif kind.strip() == "event: done":
            done = payload
    return stages, done


@pytest.fixture()
def api():
    return TestClient(app)


def test_stream_emits_only_real_stages_in_order(api):
    """Plan first, narrate last, allowlist only, no fabricated fields."""
    blocking = api.post("/chat", json={"query": SAFETY_QUERY, "session_id": "s_stream_probe"})
    if blocking.json()["state"] != "answer":
        pytest.skip("needs a working weather cache; run scripts/refresh_cache.py --weather")
    stages, done = _stream(api, SAFETY_QUERY, "s_stream_order")
    assert done["state"] == "answer"
    names = [e["stage"] for e in stages]
    assert names, "a turn with no stage events is a silent pipeline"
    assert names[0] == "plan"
    assert names[-1] == "narrate"
    assert set(names) <= set(STAGES)
    for name in ("execute", "synthesise", "verify"):
        assert name in names
    assert names.index("execute") < names.index("synthesise") < names.index("verify") < names.index("narrate")
    for event in stages:
        assert "percent" not in event
        assert "text" not in event


def test_stream_done_matches_blocking(api):
    """Same question, same answer -- the stream adds visibility, not variance."""
    session_b, session_s = "s_stream_block", "s_stream_cmp"
    blocking = api.post("/chat", json={"query": SAFETY_QUERY, "session_id": session_b}).json()
    if blocking["state"] != "answer":
        pytest.skip("needs a working weather cache; run scripts/refresh_cache.py --weather")
    _, done = _stream(api, SAFETY_QUERY, session_s)
    for key in ("state", "verdict", "verified", "numbers_checked", "degraded"):
        assert done[key] == blocking[key]
    # Evidence-basis clauses list the same sources in run-dependent order
    # (tool execution is concurrent), so compare the answer as a bag of
    # ;-separated segments rather than a string.
    seg = lambda text: sorted(s.strip() for s in text.split(";"))
    assert seg(done["answer"]) == seg(blocking["answer"])


def test_concurrent_streams_both_terminate():
    """Two overlapping streaming turns must both finish. Repro for the
    thread-safety question on SESSIONS: if this hangs, the endpoint is not
    safe under concurrent demo use."""
    results: dict[str, dict] = {}
    errors: list[str] = []

    def one(session: str) -> None:
        try:
            client = TestClient(app)
            _, done = _stream(client, "hello", session)
            results[session] = done
        except Exception as exc:  # noqa: BLE001 -- reported, then asserted empty
            errors.append(f"{session}: {exc!r}")

    threads = [threading.Thread(target=one, args=(f"s_conc_{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "a streaming turn hung under concurrency"
    assert not errors
    assert set(results) == {"s_conc_0", "s_conc_1"}
    assert all(d.get("state") == "chat" for d in results.values())


def test_verify_failure_emits_no_narrate(api, monkeypatch):
    """A poisoned claim fails verification: the trace shows verify{ok:false}
    and narration never runs. Guard order, observed over the wire."""
    probe = api.post("/chat", json={"query": SAFETY_QUERY, "session_id": "s_stream_probe2"})
    if probe.json()["state"] != "answer":
        pytest.skip("needs a working weather cache; run scripts/refresh_cache.py --weather")

    from core.schemas.recommendation import Claim, ClaimKind

    real_build = turn_module.build_recommendation

    def poisoned(execution, intent, turn_id):
        rec = real_build(execution, intent, turn_id=turn_id)
        # A cited-but-bogus figure: construction passes (evidence is just
        # strings), verification must fail (99.25 appears in no tool log).
        fake = Claim(
            id="hallucinated",
            kind=ClaimKind.OBSERVED,
            template="Waves {x} m.",
            slots={"x": 99.25},
            evidence=["tc_000_nonexistent"],
        )
        return rec.model_copy(update={"claims": [*rec.claims, fake]})

    monkeypatch.setattr(turn_module, "build_recommendation", poisoned)
    stages, done = _stream(api, SAFETY_QUERY, "s_stream_poison")
    assert done["state"] == "error"
    verifies = [e for e in stages if e["stage"] == "verify"]
    assert verifies and all(v["ok"] is False for v in verifies)
    assert "narrate" not in [e["stage"] for e in stages]
