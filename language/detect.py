"""Language detection. A stub that always returns English.

Phase one is English only. This module exists so that adding Tamil is an
adapter change rather than a change to every call site -- CLAUDE.md is explicit
that it must not be deleted.

The stub is honest about being a stub: it does not sniff the input and pretend
to have detected English, it returns "en" unconditionally and says so.
"""

from __future__ import annotations

__all__ = ["SUPPORTED", "detect_language"]

#: Languages the system can RENDER an answer in -- each one has a
#: ``language/templates/<tag>`` directory. This is deliberately not the same
#: question as which language a *question* arrives in: detection is still the
#: stub below, so Tamil is answered when the UI asks for it, not when the
#: system guesses at it.
SUPPORTED = ("en", "ta")


def detect_language(text: str) -> str:
    """Always "en". Phase one does not detect anything.

    The argument is accepted and ignored on purpose: every call site is
    already written to pass the text, so switching to a real detector later
    touches this function and nothing else.
    """
    return "en"
