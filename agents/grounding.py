"""Numbers in model prose: check them, do not delete them.

The rule this replaces
----------------------
Every string a model wrote used to pass through ``strip_numbers``, which
deletes numeric tokens outright. ``"within 20 km"`` became ``"within  km"``.
The docstring called that an acceptable trade -- a clumsy sentence beats a
fabricated safety figure -- and on its own terms it was right.

What it cost was not obvious until you read a few hundred answers. Because
digits were destroyed, every agent prompt had to say *never write a number*,
and an agent told that cannot be specific about anything, because
specificity in this domain **is** numeric. "Conditions are close to the
limit" is what a model writes when it is forbidden from writing "waves are
2.2 m against a 2.5 m limit". The blandness people complained about in the
agent chatter, the concerns and the suggestions was not a prompt-tuning
problem. It was this function, propagating backwards into seven prompts.

The replacement
---------------
``agents/narrate.py`` already had the better primitive. ``number_guard``
compares the numbers in a narration against the numbers in the verified
rendering it was shown, and **discards the narration** if one drifted. It
never edits text. That is strictly safer than stripping, because a stripped
sentence hides the fact that a model tried to invent a figure, while a
rejected one is a signal you can count.

So: a number an agent writes is kept when some tool actually returned it,
and the fragment carrying it is dropped when none did. The agent may be as
specific as its evidence allows, and no more.

Scope, deliberately
-------------------
This applies where ground truth exists -- deliberation, concerns, request
reasons, follow-up suggestions. It does **not** apply to the conversational
router: a chat turn has called no tool, so there is no evidence to check
against and no number the model could legitimately know. That path keeps
``strip_numbers`` and a prompt that says so honestly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from orchestrator.verifier import numbers_match

__all__ = [
    "MARITIME_CONSTANTS",
    "grounded",
    "numeric_tokens",
    "ungrounded_numbers",
]

#: Numeric tokens, including thousands separators and decimals. Matches the
#: shape ``strip_numbers`` used, so nothing that used to be caught escapes.
_NUMBER = re.compile(r"\d[\d,]*\.?\d*")

#: Figures that are part of the vocabulary rather than a measurement, and
#: which no tool will ever return.
#:
#: Keep this list short and justify every entry. It is a hole in the guard,
#: and the temptation will be to widen it the first time a legitimate-looking
#: sentence gets dropped. The correct response to that is almost always to
#: check why the tool did not return the number, not to add it here.
MARITIME_CONSTANTS: frozenset[float] = frozenset({
    16.0,   # VHF channel 16, the distress and calling channel. Appears in
            # operational guidance ("keep VHF on channel 16") and is a name,
            # not a quantity.
    112.0,  # India's single emergency number.
})


def numeric_tokens(text: str) -> list[str]:
    """Every numeric token in a string, as written."""
    return _NUMBER.findall(text)


def _as_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def ungrounded_numbers(text: str, allowed: Iterable[float]) -> list[str]:
    """Numbers in ``text`` that no tool output supports.

    Comparison uses the verifier's own :func:`numbers_match`, so the tolerance
    is the one the rest of the system already agreed on: a figure is accepted
    when it is a correct rounding of a real tool output *at the precision it
    was written to*. Writing 2.2 for an actual 2.24 is fine; writing 2.3 is
    not, and neither is writing 2.24 when the tool said 2.2.

    An empty result means every number in the string is real.
    """
    pool = list(allowed)
    out: list[str] = []
    for token in numeric_tokens(text):
        value = _as_float(token)
        if value is None:
            continue
        if value in MARITIME_CONSTANTS:
            continue
        if not any(numbers_match(value, actual) for actual in pool):
            out.append(token)
    return out


def grounded(text: str, allowed: Iterable[float]) -> tuple[str, list[str]]:
    """``(text, [])`` when every number is real; ``("", [bad])`` when one is not.

    Fragment-level, not turn-level. One agent inventing a figure in one
    concern costs that concern, not the deliberation around it and not the
    requests it made -- those carry structured arguments that
    ``validate_plan`` checks separately and far more strictly than any prose.

    Returning the empty string rather than raising is what lets every call
    site treat rejection the same way it already treats "the model said
    nothing useful", which is a path all of them had to have anyway.
    """
    bad = ungrounded_numbers(text, allowed)
    return ("", bad) if bad else (text, [])
