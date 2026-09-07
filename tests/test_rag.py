"""RAG explainer: disabled-by-default, number-stripped, TESTS-gated.

The three properties the plan review required:
- no env -> [] with no network (answers identical, store is a supported state);
- every failure mode -> [] (fail closed, never raises);
- retrieved wording cannot create evidence: the barrier is strip_numbers() in
  rag/store.py, and hypotheses still drop anything outside TESTS.
"""

from __future__ import annotations

from unittest.mock import patch

from agents.hypotheses import TESTS, propose
from orchestrator.llm.client import LLMResult
from rag.corpus import build_corpus
from rag.store import background_for_causal, enabled, retrieve


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


def _no_network(*args, **kwargs):
    raise AssertionError("network call made while disabled")


def test_disabled_without_env_and_touches_no_network(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert enabled() is False
    assert retrieve("anything", _post=_no_network) == []
    assert background_for_causal("Nagapattinam") == []


def test_retrieved_numbers_are_stripped_in_code(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "key")
    rows = [{
        "source": "risk_thresholds.yaml#x",
        "title": "Wave limit note",
        "body": "Waves over 2.5 m exceed the 25 kn operating picture.",
    }]
    notes = retrieve("wave limit", _post=lambda *a, **k: _Resp(200, rows))
    assert len(notes) == 1
    assert "Wave limit note" in notes[0]
    assert not any(ch.isdigit() for ch in notes[0]), notes[0]


def test_every_failure_mode_returns_empty(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "key")
    assert retrieve("x", _post=lambda *a, **k: _Resp(500, [])) == []
    assert retrieve("x", _post=lambda *a, **k: _Resp(200, [], bad_json=True)) == []
    assert retrieve("x", _post=lambda *a, **k: _Resp(200, {"not": "a list"})) == []

    def _boom(*args, **kwargs):
        raise ConnectionError("down")

    assert retrieve("x", _post=_boom) == []
    assert retrieve("   ", _post=_no_network) == []


def test_corpus_comes_from_cited_in_repo_text_only():
    docs = build_corpus()
    assert len(docs) >= 10
    sources = {d.source for d in docs}
    assert any(s.startswith("risk_thresholds.yaml#") for s in sources)
    assert "ingest/static/boundaries.py#transcription" in sources
    assert "ingest/climatology.py#sensor-choice" in sources
    for doc in docs:
        assert doc.title.strip() and doc.body.strip()


def test_migration_creates_guarded_fts_table():
    sql = open("db/migrations/001_rag.sql", encoding="utf-8").read()
    assert "create table if not exists rag_docs" in sql
    assert "create extension if not exists vector" in sql
    assert "using gin" in sql
    assert "generated always as (to_tsvector" in sql


def test_background_cannot_smuggle_an_untestable_hypothesis():
    from datetime import datetime, timezone

    from core.provenance import ToolCallLog
    from orchestrator.executor import ExecutionResult

    log = ToolCallLog(turn_id="t_test")
    result = ExecutionResult(log=log, outputs={}, call_ids={})
    reply = LLMResult(
        ok=True, provider="test", model="test",
        text='{"hypotheses": [{"test_id": "the_moon", "statement": "lunar rays scatter the fish 42 leagues away"}]}',
    )
    with patch("agents.hypotheses.llm.complete", return_value=reply):
        proposals, _ = propose(
            result, "Nagapattinam",
            background=["Moon note -- lunar rays scatter things far away."],
        )
    assert proposals == []
    assert set(TESTS) == {
        "low_productivity", "bloom_or_turbidity",
        "weak_frontal_structure", "no_reachable_zone",
    }
