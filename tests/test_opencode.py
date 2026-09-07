"""OpenCode Zen provider: fast-fail, fall-through, and shape discipline.

The load-bearing properties, per the plan review:
- no OPENCODE_API_KEY (or a blank one) returns ok=False with NO socket call,
  so offline runs and every existing test pass through instantly;
- a 429 on the sole Zen key falls through to the next tier, never retries Zen;
- only the /v1/chat/completions shape is parsed; anything else is ok=False.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from orchestrator.llm import client


def _payload(text="hello from zen", finish="stop"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body="rate limited"):
    return urllib.error.HTTPError(
        "https://opencode.ai/zen/v1/chat/completions",
        code,
        "error",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def _no_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("network call made without a key")

    monkeypatch.setattr("urllib.request.urlopen", _boom)


def test_missing_key_fast_fails_without_network(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    _no_network(monkeypatch)
    result = client._complete_opencode("planner", "sys", "user")
    assert not result.ok
    assert "not set" in (result.error or "")


def test_blank_key_fast_fails_without_network(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "   ")
    _no_network(monkeypatch)
    result = client._complete_opencode("planner", "sys", "user")
    assert not result.ok


def test_ok_path_reports_provider_model_and_usage(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _FakeResponse(_payload())
    )
    result = client._complete_opencode("planner", "sys", "user")
    assert result.ok
    assert result.provider == "opencode"
    assert result.model == "kimi-k2.6"
    assert result.text == "hello from zen"
    assert result.input_tokens == 10
    assert result.output_tokens == 5


def test_rate_limit_falls_through_to_groq(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return _FakeResponse(_payload("via groq"))

    monkeypatch.setattr("urllib.request.urlopen", _flaky)
    result = client.complete("deliberator", "sys", "user")
    assert result.ok
    assert result.provider == "groq"
    assert result.text == "via groq"


def test_invalid_key_is_terminal_for_opencode(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "bad-key")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def _denied(*args, **kwargs):
        raise _http_error(401, "unauthorized")

    monkeypatch.setattr("urllib.request.urlopen", _denied)
    result = client._complete_opencode("planner", "sys", "user")
    assert not result.ok
    assert "invalid API key" in (result.error or "")


def test_empty_response_is_failure(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _FakeResponse(_payload(""))
    )
    result = client._complete_opencode("narrator", "sys", "user")
    assert not result.ok
    assert "empty response" in (result.error or "")


def test_non_chat_completions_shape_is_rejected(monkeypatch):
    # A /v1/responses-style body has no choices[0].message: it must fail,
    # never be parsed optimistically.
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse({"output": [{"content": "hi"}]}),
    )
    result = client._complete_opencode("planner", "sys", "user")
    assert not result.ok
    assert "unexpected response shape" in (result.error or "")


def test_all_three_roles_configured():
    from core import config

    section = config.load_yaml("models.yaml")["opencode"]
    for role in ("planner", "narrator", "deliberator"):
        assert section[role]["model"], f"opencode.{role} has no model"
        assert int(section[role]["max_tokens"]) > 0


def test_provider_status_reports_opencode(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    assert client.provider_status()["opencode"] is True
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    assert client.provider_status()["opencode"] is False
