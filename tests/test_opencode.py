"""Zen + Go providers: fast-fail, chains, transports, fall-through.

Load-bearing properties:
- no key (either gateway) -> ok=False with NO socket call;
- per-role model chains tried in order; 429/shape/empty move on, 401 is
  terminal for the gateway;
- Muse Spark ids ride /v1/responses, deepseek/glm ride chat/completions;
- chain exhaustion falls through to the next provider, never raises.
"""

from __future__ import annotations

import io
import json
import urllib.error

from orchestrator.llm import client


def _chat_payload(text="hello from zen", finish="stop"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _responses_payload(text="hello from spark"):
    return {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": text}],
        }],
        "usage": {"input_tokens": 12, "output_tokens": 6},
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


def _http_error(code, url="https://example.invalid", body="err"):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body.encode()))


class _Script:
    """Scripted urlopen: outcomes in order, requests recorded."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    def bodies(self):
        return [json.loads(r.data.decode("utf-8")) for r in self.requests]

    def urls(self):
        return [r.full_url for r in self.requests]


def _no_network(*args, **kwargs):
    raise AssertionError("network call made without a key")


def _env_off(monkeypatch):
    for var in (
        "OPENCODE_API_KEY", "OPENCODE_GO_API_KEY", "GROQ_API_KEY",
        "GROQ_API_KEY_2", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


def test_missing_keys_fast_fail_without_network(monkeypatch):
    _env_off(monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    assert client._complete_opencode("planner", "s", "u").error == "OPENCODE_API_KEY not set"
    assert client._complete_opencode_go("planner", "s", "u").error == "OPENCODE_GO_API_KEY not set"


def test_blank_keys_fast_fail(monkeypatch):
    _env_off(monkeypatch)
    monkeypatch.setenv("OPENCODE_API_KEY", "   ")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "  ")
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    assert not client._complete_opencode("planner", "s", "u").ok
    assert not client._complete_opencode_go("planner", "s", "u").ok


def test_transport_routing_by_model_id():
    assert client._zen_transport("muse-spark-1.3-contributor-free") == "responses"
    assert client._zen_transport("muse-spark-1.2") == "responses"
    assert client._zen_transport("deepseek-v4-flash") == "chat"
    assert client._zen_transport("kimi-k2.6") == "chat"


def test_spark_first_id_answers_via_responses(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _Script(_responses_payload())
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client._complete_opencode("planner", "sys", "user")
    assert result.ok
    assert result.provider == "opencode"
    assert result.model == "muse-spark-1.3-contributor-free"
    assert result.text == "hello from spark"
    assert result.input_tokens == 12 and result.output_tokens == 6
    assert script.urls() == ["https://opencode.ai/zen/v1/responses"]
    assert script.bodies()[0]["max_output_tokens"] == 4096


def test_chain_falls_to_next_id_with_per_id_transport(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _Script(
        _http_error(429, "https://opencode.ai/zen/v1/responses"),
        _http_error(429, "https://opencode.ai/zen/v1/responses"),
        _http_error(429, "https://opencode.ai/zen/v1/responses"),
        _http_error(429, "https://opencode.ai/zen/v1/responses"),
        _chat_payload("via deepseek"),
    )
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client._complete_opencode("deliberator", "sys", "user")
    assert result.ok and result.model == "deepseek-v4-flash"
    assert script.bodies()[0]["model"] == "muse-spark-1.3-contributor-free"
    assert script.urls()[:4] == ["https://opencode.ai/zen/v1/responses"] * 4
    assert script.urls()[4] == "https://opencode.ai/zen/v1/chat/completions"


def test_invalid_key_is_terminal_for_the_gateway(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "bad-key")
    script = _Script(_http_error(401, body="unauthorized"))
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client._complete_opencode("planner", "sys", "user")
    assert not result.ok and "invalid API key" in (result.error or "")
    assert len(script.requests) == 1, "a bad key must not walk the chain"


def test_chain_exhaustion_falls_through_to_groq(monkeypatch):
    _env_off(monkeypatch)
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    script = _Script(*([_http_error(429)] * 5 + [_chat_payload("via groq")]))
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client.complete("deliberator", "sys", "user")
    assert result.ok and result.provider == "groq"
    assert len(script.requests) == 6


def test_go_gateway_ok_path_and_last_fallback(monkeypatch):
    _env_off(monkeypatch)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    script = _Script(_http_error(429), _chat_payload("via glm"))
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client._complete_opencode_go("narrator", "sys", "user")
    assert result.ok and result.model == "glm-5.3-flash"
    assert script.urls() == ["https://opencode.ai/zen/go/v1/chat/completions"] * 2
    assert [b["model"] for b in script.bodies()] == ["deepseek-v4-flash", "glm-5.3-flash"]


def test_go_responses_shape_rejected_chat_forced(monkeypatch):
    # Go speaks chat/completions only: a responses-shaped body there is a
    # shape error, and the chain still ends on glm.
    _env_off(monkeypatch)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    script = _Script({"output": [{"content": "hi"}]}, _chat_payload("via glm"))
    monkeypatch.setattr("urllib.request.urlopen", script)
    result = client._complete_opencode_go("narrator", "sys", "user")
    assert result.ok and result.model == "glm-5.3-flash"


def test_empty_responses_run_is_failure():
    parsed = client._parse_responses({"status": "incomplete", "output": []}, "opencode", "m")
    assert not parsed.ok


def test_chains_and_roles_configured():
    from core import config

    section = config.load_yaml("models.yaml")["opencode"]
    assert section["models"][:2] == ["muse-spark-1.3-contributor-free", "muse-spark-1.3"]
    assert section["models"][-1] == "deepseek-v4-flash"
    for role in ("planner", "narrator", "deliberator"):
        assert int(section[role]["max_tokens"]) > 0
    go = config.load_yaml("models.yaml")["opencode-go"]
    assert go["models"] == ["deepseek-v4-flash", "glm-5.3-flash"]
    order = config.load_yaml("models.yaml")["provider_order"]
    assert order.index("opencode") < order.index("opencode-go") < order.index("groq")


def test_provider_status_reports_both_gateways(monkeypatch):
    _env_off(monkeypatch)
    assert client.provider_status()["opencode"] is False
    assert client.provider_status()["opencode-go"] is False
    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k2")
    assert client.provider_status()["opencode"] is True
    assert client.provider_status()["opencode-go"] is True
