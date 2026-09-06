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
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "anthropic": bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "ollama": _ollama_reachable(),
    }


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
