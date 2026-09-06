"""Language adapter. Pass-through in phase one, kept so Tamil is one change."""

from __future__ import annotations

from language.detect import SUPPORTED, detect_language
from language.translate import translate, translate_template

__all__ = ["detect_language", "translate", "translate_template", "SUPPORTED", "render"]


def render(rec, language: str = "en") -> str:
    """Render a recommendation in the target language."""
    if language != "en":
        raise NotImplementedError(
            f"No templates for {language!r}. Phase one is English only."
        )
    from language.templates.en import render as render_en

    return render_en(rec)
