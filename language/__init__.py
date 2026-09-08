"""Language adapter. Pass-through in phase one, kept so Tamil is one change."""

from __future__ import annotations

from language.detect import SUPPORTED, detect_language
from language.translate import translate, translate_template

__all__ = ["detect_language", "translate", "translate_template", "SUPPORTED", "render"]


def render(rec, language: str = "en") -> str:
    """Render a recommendation in the target language.

    Dispatch only. Each ``templates/<lang>`` module owns its own prose and
    its own catalogue; adding a language is a directory and one line here,
    which is the property CLAUDE.md asked the stub to preserve.

    An unknown tag still raises. Returning English under a Tamil flag would
    be the one failure mode this adapter exists to prevent -- the caller
    (``orchestrator.turn``) decides to fall back, and records that it did.
    """
    if language == "en":
        from language.templates.en import render as render_en

        return render_en(rec)
    if language == "ta":
        from language.templates.ta import render as render_ta

        return render_ta(rec)
    raise NotImplementedError(
        f"No templates for {language!r}. Supported: {', '.join(SUPPORTED)}."
    )
