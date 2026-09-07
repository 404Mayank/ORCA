"""Tiered LLM client: Anthropic first, Ollama when the venue loses internet.

One small surface -- :func:`complete` -- so the agents never touch a provider
SDK directly. Swapping providers, or adding Groq, is a change here and nowhere
else.

Two things this module refuses to do, because CLAUDE.md puts them in code:

* It does not retry its way past a refusal or a malformed response by
  loosening the request. A failed completion returns :class:`LLMResult` with
  ``ok=False`` and the caller decides -- for the narrator that means falling
  back to the deterministic template renderer, which is always correct.
* It never raises on provider failure. The venue will lose internet, and a
  traceback in the middle of a demo is worse than a slightly stiffer sentence.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

from core import config

__all__ = ["LLMResult", "Role", "complete", "provider_status"]

Role = Literal["planner", "narrator", "deliberator"]
"""Three roles, separated because they carry different risk.

``planner`` chooses tools; its output is validated before anything runs.
``narrator`` rewrites a verified object; its numbers are guarded.
``deliberator`` is a domain agent reasoning about its own results -- what it
found, whether it is enough, and what it needs from another agent. Its output
is a set of *requests*, each validated like any other plan step, and a set of
*concerns* in words. **It never emits a number that reaches a claim.**
"""


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    text: str = ""
    provider: str = "none"
    model: str = ""
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _models() -> dict:
    return config.load_yaml("models.yaml")


def provider_status() -> dict[str, bool]:
    """Which providers look usable right now. Cheap; no request is made."""
    return {
        "opencode": bool(os.environ.get("OPENCODE_API_KEY", "").strip()),
        "opencode-go": bool(os.environ.get("OPENCODE_GO_API_KEY", "").strip()),
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "anthropic": bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "ollama": _ollama_reachable(),
    }


# --------------------------------------------------------------------------
# OpenCode Zen -- the unlimited tier, plus the Go gateway fallback
# --------------------------------------------------------------------------
#
# Two gateways, both keyed separately:
#
# * Zen (`https://opencode.ai/zen/v1`) -- model roster at /v1/models (public,
#   no auth). Two transports: /v1/chat/completions (deepseek, kimi, glm,
#   minimax) and /v1/responses (Muse Spark, GPT, Grok families -- OpenAI
#   Responses API shape, NOT chat completions). /v1/messages (Claude) is not
#   called; the anthropic provider covers Claude natively.
# * Go (`https://opencode.ai/zen/go/v1`) -- OpenAI-completions transport only
#   (deepseek-v4-flash confirmed there). Key in OPENCODE_GO_API_KEY.
#
# Model ids verified against the live public roster on 2026-09-07:
# muse-spark-1.3, muse-spark-1.2, muse-spark-1.3-contributor-free,
# muse-spark-1.2-contributor-free, deepseek-v4-flash. Re-list before a demo --
# gateway rosters move, and an unlisted id fails as a quiet fall-through.
#
# PRIVACY: *-contributor-free models are free in exchange for training on
# prompts/completions (Zen docs, Privacy). Fishermen queries carry locations
# but no PII; the tradeoff is documented here, not hidden. Prefer paid
# siblings for anything sensitive.
#
# Ordering note: `provider_order` lists opencode first, but an unconfigured
# provider costs nothing -- a missing/blank key returns ok=False before any
# socket call, so offline/CI runs fall through instantly.

_ZEN_CHAT_URL = "https://opencode.ai/zen/v1/chat/completions"
_ZEN_RESPONSES_URL = "https://opencode.ai/zen/v1/responses"
_ZEN_GO_URL = "https://opencode.ai/zen/go/v1/chat/completions"

#: Model id prefixes served on the Responses transport. Everything else on a
#: Zen-family gateway uses chat/completions. Prefix routing, not per-model
#: config, so a renamed model fails loudly as an unexpected shape rather than
#: silently hitting the wrong endpoint.
_RESPONSES_PREFIXES = ("muse-spark-", "gpt-", "grok-")


def _zen_transport(model: str) -> str:
    """'responses' or 'chat' for a Zen model id."""
    if model.startswith(_RESPONSES_PREFIXES):
        return "responses"
    return "chat"


def _post_json(url: str, key: str, body: dict, provider: str, model: str) -> dict | LLMResult:
    """POST and parse JSON, or an LLMResult on transport failure."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=float(_models()["limits"]["timeout_s"])
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        if exc.code == 401:
            # Key-level: applies to every model on this gateway. Terminal.
            return LLMResult(ok=False, provider=provider, model=model, error="invalid API key")
        if exc.code == 403:
            return LLMResult(ok=False, provider=provider, model=model, error=f"forbidden (key or model access): {detail}")
        return LLMResult(ok=False, provider=provider, model=model, error=f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        return LLMResult(ok=False, provider=provider, model=model, error=f"connection: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return LLMResult(ok=False, provider=provider, model=model, error=f"{type(exc).__name__}: {exc}")


def _parse_chat(payload: dict, provider: str, model: str) -> LLMResult:
    try:
        choice = payload["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return LLMResult(ok=False, provider=provider, model=model, error=f"unexpected response shape: {str(payload)[:200]}")
    if finish == "content_filter":
        return LLMResult(ok=False, provider=provider, model=model, error="refused by content filter")
    if not text:
        return LLMResult(ok=False, provider=provider, model=model, error=f"empty response (finish_reason={finish})")
    usage = payload.get("usage") or {}
    return LLMResult(
        ok=True, text=text, provider=provider, model=model,
        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
    )


def _parse_responses(payload: dict, provider: str, model: str) -> LLMResult:
    """OpenAI Responses API shape: output[].content[].output_text.text."""
    try:
        parts: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") == "output_text":
                    parts.append(str(block.get("text") or ""))
        text = "".join(parts).strip()
    except (AttributeError, TypeError):
        return LLMResult(ok=False, provider=provider, model=model, error=f"unexpected response shape: {str(payload)[:200]}")
    if not text:
        status = payload.get("status")
        if status and status != "completed":
            return LLMResult(ok=False, provider=provider, model=model, error=f"responses run {status}")
        return LLMResult(ok=False, provider=provider, model=model, error=f"unexpected response shape: {str(payload)[:200]}")
    usage = payload.get("usage") or {}
    return LLMResult(
        ok=True, text=text, provider=provider, model=model,
        input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
    )


def _complete_opencode(role: Role, system: str, user: str) -> LLMResult:
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        # Fast-fail with no network: every offline run and every existing
        # test that mocks a later tier passes through here first.
        return LLMResult(ok=False, provider="opencode", error="OPENCODE_API_KEY not set")
    try:
        models = list(_models()["opencode"]["models"])
    except (KeyError, TypeError) as exc:
        return LLMResult(ok=False, provider="opencode", error=f"no model chain configured: {exc}")
    if not models:
        return LLMResult(ok=False, provider="opencode", error="no model chain configured")
    return _complete_chain("opencode", key, models, role, system, user, _ZEN_CHAT_URL, _ZEN_RESPONSES_URL)


def _complete_chain(provider, key, models, role, system, user, chat_url, responses_url=None):
    # One model chain; URL chosen per model id. Never raises.
    try:
        params = _models()[provider][role]
        temperature = float(params.get("temperature", 0.2))
        max_tokens = int(params["max_tokens"])
    except (KeyError, TypeError) as exc:
        return LLMResult(ok=False, provider=provider, error=f"no params configured for role {role!r}: {exc}")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last: LLMResult = LLMResult(ok=False, provider=provider, error="no models configured")
    for pos, model in enumerate(models):
        if _zen_transport(model) == "responses":
            url, body, parse = (
                responses_url,
                {"model": model, "input": messages, "temperature": temperature, "max_output_tokens": max_tokens},
                _parse_responses,
            )
        else:
            url, body, parse = (
                chat_url,
                {"model": model, "messages": messages, "temperature": temperature, "max_completion_tokens": max_tokens},
                _parse_chat,
            )
        payload = _post_json(url, key, body, provider, model)
        if isinstance(payload, LLMResult):
            last = payload
            # 401 on the FIRST id means the key itself is bad: terminal for
            # the gateway. 401 on a later id is model-level access (e.g. a
            # contributor key asking for a paid sibling): the remaining ids
            # may still serve, so the chain walks on. Found live 2026-09-07.
            if payload.error == "invalid API key" and pos == 0:
                break
            continue
        last = parse(payload, provider, model)
        if last.ok:
            return last
    return last


def _complete_opencode_go(role: Role, system: str, user: str) -> LLMResult:
    key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if not key:
        return LLMResult(ok=False, provider="opencode-go", error="OPENCODE_GO_API_KEY not set")
    try:
        models = list(_models()["opencode-go"]["models"])
    except (KeyError, TypeError) as exc:
        return LLMResult(ok=False, provider="opencode-go", error=f"no model chain configured: {exc}")
    if not models:
        return LLMResult(ok=False, provider="opencode-go", error="no model chain configured")
    # Go gateway speaks chat/completions only (confirmed via served model
    # metadata); force that transport for every id in its chain.
    return _complete_chain("opencode-go", key, models, role, system, user, _ZEN_GO_URL, None)



# --------------------------------------------------------------------------
# Groq -- the fast, cheap tier CLAUDE.md asks for
# --------------------------------------------------------------------------
#
# Groq exposes an OpenAI-compatible chat endpoint. Called over urllib rather
# than through a vendor SDK so the offline tier and this one share one code
# path and one failure model. Two facts learned the hard way, 2026-09-04:
#
#   * Cloudflare in front of api.groq.com rejects urllib's default User-Agent
#     with HTTP 403 / error 1010. A real UA header is required, not optional.
#   * The models CLAUDE.md named for this tier -- Llama 3.3 70B and Mistral
#     Large -- are NOT on the current roster. Verified by listing /models with
#     this key: the text models present are openai/gpt-oss-120b, gpt-oss-20b,
#     qwen3.6-27b, qwen3.8-27b and groq/compound. See docs/verified_sources.md.

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_UA = "orca/0.1 (+python-urllib)"


def _groq_keys() -> list[str]:
    """Every configured Groq key, in order.

    ``GROQ_API_KEY`` first, then ``GROQ_API_KEY_2``, ``_3`` and so on.

    Several keys exist because of arithmetic, not paranoia: once the domain
    agents deliberate, a single turn makes up to six calls -- planner, one per
    active agent, narrator -- and a free-tier key is rate limited well before
    that becomes comfortable under demo conditions. A rate limit on one key
    falls through to the next; only when all are exhausted does the tier fail
    and the next provider get its turn.
    """
    keys = [os.environ.get("GROQ_API_KEY", "").strip()]
    index = 2
    while (extra := os.environ.get(f"GROQ_API_KEY_{index}", "").strip()):
        keys.append(extra)
        index += 1
    return [key for key in keys if key]


#: Rotates the starting key between calls, so load is spread rather than
#: always hammering the first key until it 429s. Module-level and unsynchronised
#: on purpose: a race here costs a wasted retry, not correctness.
_KEY_CURSOR = {"n": 0}


def _complete_groq(role: Role, system: str, user: str) -> LLMResult:
    keys = _groq_keys()
    if not keys:
        return LLMResult(ok=False, provider="groq", error="GROQ_API_KEY not set")

    spec = _models()["groq"][role]
    model = spec["model"]
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(spec.get("temperature", 0.2)),
        "max_completion_tokens": int(spec["max_tokens"]),
    }
    # gpt-oss models are reasoning models; without this they think at length
    # about a paragraph rewrite. Other models reject the field, so it is gated.
    if "gpt-oss" in model and spec.get("reasoning_effort"):
        body["reasoning_effort"] = spec["reasoning_effort"]

    payload = None
    last_error = "no key attempted"
    start = _KEY_CURSOR["n"] % len(keys)
    _KEY_CURSOR["n"] = start + 1

    for offset in range(len(keys)):
        key = keys[(start + offset) % len(keys)]
        request = urllib.request.Request(
            _GROQ_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=float(_models()["limits"]["timeout_s"])
            ) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code == 429:
                # This key is spent for now. Try the next one rather than
                # dropping to a slower provider -- or, worse, to no LLM at all
                # while a perfectly good key sits unused.
                last_error = f"rate limited: {detail}"
                continue
            if exc.code == 401:
                # A bad key is a configuration error, not a transient one, but
                # the others may still be fine.
                last_error = "invalid API key"
                continue
            return LLMResult(
                ok=False, provider="groq", model=model, error=f"HTTP {exc.code}: {detail}"
            )
        except urllib.error.URLError as exc:
            # The network is down, not the key. Trying the others would just
            # wait for the same timeout again.
            return LLMResult(
                ok=False, provider="groq", model=model, error=f"connection: {exc.reason}"
            )
        except Exception as exc:  # noqa: BLE001
            return LLMResult(
                ok=False, provider="groq", model=model, error=f"{type(exc).__name__}: {exc}"
            )

    if payload is None:
        suffix = f" (all {len(keys)} keys)" if len(keys) > 1 else ""
        return LLMResult(ok=False, provider="groq", model=model, error=last_error + suffix)

    try:
        choice = payload["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return LLMResult(ok=False, provider="groq", model=model, error=f"unexpected response shape: {str(payload)[:200]}")

    if finish == "content_filter":
        return LLMResult(ok=False, provider="groq", model=model, error="refused by content filter")
    if not text:
        return LLMResult(ok=False, provider="groq", model=model, error=f"empty response (finish_reason={finish})")

    usage = payload.get("usage") or {}
    return LLMResult(
        ok=True,
        text=text,
        provider="groq",
        model=model,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
    )


def _ollama_reachable() -> bool:
    base = _models()["ollama"]["base_url"].rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=1.0):
            return True
    except Exception:  # noqa: BLE001 - reachability probe
        return False


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


def _complete_anthropic(role: Role, system: str, user: str) -> LLMResult:
    try:
        import anthropic
    except ImportError:
        return LLMResult(ok=False, provider="anthropic", error="anthropic SDK not installed")

    spec = _models()["anthropic"][role]
    limits = _models()["limits"]
    model = spec["model"]

    try:
        client = anthropic.Anthropic(
            timeout=float(limits["timeout_s"]), max_retries=int(limits["max_retries"])
        )
        response = client.messages.create(
            model=model,
            max_tokens=int(spec["max_tokens"]),
            output_config={"effort": spec.get("effort", "high")},
            system=[
                {
                    "type": "text",
                    "text": system,
                    # The system prompt is stable across calls; the object
                    # being narrated is what varies. Cache the stable prefix.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        return LLMResult(ok=False, provider="anthropic", model=model, error="invalid API key")
    except anthropic.RateLimitError as exc:
        retry_after = exc.response.headers.get("retry-after", "?")
        return LLMResult(
            ok=False, provider="anthropic", model=model, error=f"rate limited; retry after {retry_after}s"
        )
    except anthropic.APIStatusError as exc:
        return LLMResult(
            ok=False, provider="anthropic", model=model, error=f"API {exc.status_code}: {exc.message}"
        )
    except anthropic.APIConnectionError as exc:
        return LLMResult(ok=False, provider="anthropic", model=model, error=f"connection: {exc}")

    if response.stop_reason == "refusal":
        detail = response.stop_details.explanation if response.stop_details else "no detail"
        return LLMResult(ok=False, provider="anthropic", model=model, error=f"refused: {detail}")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        return LLMResult(ok=False, provider="anthropic", model=model, error="empty response")

    return LLMResult(
        ok=True,
        text=text,
        provider="anthropic",
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# --------------------------------------------------------------------------
# Ollama -- the offline tier
# --------------------------------------------------------------------------


def _complete_ollama(role: Role, system: str, user: str) -> LLMResult:
    cfg = _models()["ollama"]
    model = cfg[role]["model"]
    base = cfg["base_url"].rstrip("/")
    payload = json.dumps(
        {
            "model": model,
            "system": system,
            "prompt": user,
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/generate", data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=float(_models()["limits"]["timeout_s"])) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return LLMResult(ok=False, provider="ollama", model=model, error=f"unreachable: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return LLMResult(ok=False, provider="ollama", model=model, error=f"{type(exc).__name__}: {exc}")

    text = (body.get("response") or "").strip()
    if not text:
        return LLMResult(ok=False, provider="ollama", model=model, error="empty response")
    return LLMResult(ok=True, text=text, provider="ollama", model=model)


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------


def complete(role: Role, system: str, user: str) -> LLMResult:
    """Run a completion through the configured tier. Never raises.

    Tries the primary provider, then the fallback. Returns ``ok=False`` with a
    reason if both fail; the caller owns what happens next.
    """
    models = _models()
    order = list(models.get("provider_order") or [models["provider"], models.get("fallback_provider")])
    backends = {
        "opencode": _complete_opencode,
        "opencode-go": _complete_opencode_go,
        "groq": _complete_groq,
        "anthropic": _complete_anthropic,
        "ollama": _complete_ollama,
    }

    errors: list[str] = []
    for name in order:
        if not name or name not in backends:
            continue
        result = backends[name](role, system, user)
        if result.ok:
            return result
        errors.append(f"{name}: {result.error}")

    return LLMResult(ok=False, error="; ".join(errors) or "no provider configured")
