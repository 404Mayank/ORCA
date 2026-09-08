"""Tamil rendering of a recommendation object.

The mirror of ``language/templates/en``. It formats; it never computes, and
it never regenerates a number.

Why a catalogue of templates rather than a translator
-----------------------------------------------------
A claim is stored as a pattern plus its slots (``core.schemas.recommendation.
Templated``). This module translates the **pattern** and copies the **slots**
through untouched, so a wave height that survived the verifier reaches a Tamil
reader as the same digits it left Python as. Passing the finished sentence
"Waves 1.8-2.2 m" through any translator invites 1.8 back as ``ஒன்றரை`` or as
2, and neither is a rounding error a fisherman can see.

Digits stay ASCII on purpose. Tamil has its own numerals (௧௨௩), and rendering
them would be more idiomatic and less safe: every downstream number check --
``agents.narrate.number_guard``, the verifier's own walk over the tool log --
matches on the literal token. A number the guard cannot see is a number
nothing is checking.

What is honestly still English
------------------------------
Three classes of text are not in the catalogue and cannot be, this slice:

* **Model-authored hypothesis statements.** ``Hypothesis.template`` is written
  by the LLM (with its numbers stripped) and differs every turn, so there is
  no key to look up.
* **The technical footer** -- ``confidence.basis`` and caveats -- which is
  composed from provenance strings ("MUR L4; 7-day composite").
* **Degradation detail** carried inside a slot, such as a tool's error text.

The renderer notices when any of these appear and says so in Tamil, in the
answer, rather than letting a half-Tamil paragraph pass for a Tamil one.
"""

from __future__ import annotations

import re

from core.schemas.recommendation import Recommendation, VerdictValue

__all__ = ["SLOT_PHRASES", "TEMPLATES", "render", "render_headline"]


_VERDICT_LINE = {
    VerdictValue.GO: "கடலுக்குச் செல்வது பாதுகாப்பானது.",
    VerdictValue.MARGINAL: "எல்லைநிலை - செல்லலாம், ஆனால் நேரத்தைக் கவனியுங்கள்.",
    VerdictValue.NO_GO: "கடலுக்குச் செல்ல வேண்டாம்.",
}

#: Same rule as the English renderer: a ratio has no unit worth speaking.
_SILENT_UNITS = {"fraction", "index", "count"}


#: English template -> Tamil template. The keys are the exact literals
#: authored in ``agents/synthesis_agent.py``; ``tests/test_tamil_render.py``
#: walks that module's AST and fails when a literal here has no counterpart
#: there, or the other way round. That test is the whole anti-drift
#: mechanism -- without it this dictionary rots the first time someone
#: rewords an English sentence.
#:
#: Every entry carries the same ``{slots}`` as its key. A translation that
#: renamed ``{min}`` would raise at render time, and the parity test asserts
#: the slot sets match before that can reach anyone.
TEMPLATES: dict[str, str] = {
    # --- verdict headlines -------------------------------------------------
    "Safe to go out.": _VERDICT_LINE[VerdictValue.GO],
    "Marginal - you can go, but watch the time.": _VERDICT_LINE[VerdictValue.MARGINAL],
    "Do not go out.": _VERDICT_LINE[VerdictValue.NO_GO],

    # --- driver labels -----------------------------------------------------
    "Waves {min}-{max} {unit}": "அலைகள் {min}-{max} {unit}",
    "Wind {min}-{max} {unit}": "காற்று {min}-{max} {unit}",
    "Visibility {min}-{max} {unit}": "பார்வைத்தூரம் {min}-{max} {unit}",
    "Wave steepness {max}": "அலைச் செங்குத்தளவு {max}",
    "{min}-{max} {unit}": "{min}-{max} {unit}",

    # --- safety claims -----------------------------------------------------
    "Waves {min}-{max} {unit}.": "அலைகள் {min}-{max} {unit}.",
    "Wind {min}-{max} {unit}.": "காற்று {min}-{max} {unit}.",
    "Verdict downgraded: {reason}.": "தீர்ப்பு தாழ்த்தப்பட்டது: {reason}.",
    "Start back by {turn_back} to be ashore by {ashore_by}.":
        "{ashore_by} மணிக்குக் கரை சேர {turn_back} மணிக்குத் திரும்பத் தொடங்குங்கள்.",
    "Could not assess conditions - the safety calculation did not run.":
        "நிலைமையை மதிப்பிட முடியவில்லை - பாதுகாப்புக் கணக்கீடு இயங்கவில்லை.",

    # --- negative findings -------------------------------------------------
    "No cyclone or depression active in the Bay of Bengal.":
        "வங்காள விரிகுடாவில் புயலோ காற்றழுத்தத் தாழ்வுநிலையோ இல்லை.",
    "Tide is not a constraint{detail}.": "அலை ஓட்டம் தடையாக இல்லை{detail}.",

    # --- fishing zones -----------------------------------------------------
    "No potential fishing zone met the criteria within range today.":
        "இன்று வரம்பிற்குள் எந்த மீன்பிடி வலயமும் தகுதிபெறவில்லை.",
    "No sea-surface temperature front strong enough to aggregate fish was found within range.":
        "மீன்கள் திரளும் அளவுக்கு வலுவான கடல்மேற்பரப்பு வெப்பநிலை முனை "
        "வரம்பிற்குள் காணப்படவில்லை.",
    "Best fishing zone is {distance_km} km {bearing_deg}° from port.":
        "சிறந்த மீன்பிடி வலயம் துறைமுகத்திலிருந்து {bearing_deg}° திசையில் "
        "{distance_km} km தொலைவில் உள்ளது.",
    "Candidate zone {distance_km} km at bearing {bearing_deg}°.":
        "வாய்ப்புள்ள வலயம் {bearing_deg}° திசையில் {distance_km} km தொலைவில்.",
    "Another candidate zone lies {distance_km} km at {bearing_deg}°.":
        "மற்றொரு வாய்ப்புள்ள வலயம் {bearing_deg}° திசையில் {distance_km} km "
        "தொலைவில் உள்ளது.",
    "Sea-surface temperature changes {gradient} °C per km across the front.":
        "முனையைக் கடக்கும்போது கடல்மேற்பரப்பு வெப்பநிலை ஒரு km-க்கு "
        "{gradient} °C மாறுகிறது.",
    "Chlorophyll there is {chl} mg/m3.": "அங்கு பச்சையம் {chl} mg/m3 உள்ளது.",
    "Check the safety forecast before leaving.":
        "புறப்படும் முன் பாதுகாப்பு முன்னறிவிப்பைப் பாருங்கள்.",

    # --- boundaries --------------------------------------------------------
    "Clear of {zones}.": "{zones} எல்லையிலிருந்து விலகி உள்ளீர்கள்.",
    "Departure point is clear of {zones}.":
        "புறப்படும் இடம் {zones} எல்லையிலிருந்து விலகி உள்ளது.",
    "No boundary breach against {zones}.": "{zones} எல்லை மீறல் எதுவும் இல்லை.",
    "Inside or close to {zone} - do not cross.":
        "{zone} எல்லைக்குள் அல்லது அதற்கு அருகில் உள்ளீர்கள் - கடக்க வேண்டாம்.",
    "{zone} is {distance_km} km away.": "{zone} {distance_km} km தொலைவில் உள்ளது.",
    "Crossing the International Maritime Boundary Line risks arrest and seizure of the vessel.":
        "சர்வதேச கடல் எல்லைக் கோட்டைக் கடந்தால் கைது மற்றும் படகு "
        "பறிமுதல் அபாயம் உள்ளது.",

    # --- causal ------------------------------------------------------------
    "Chlorophyll is {min_mg} to {max_mg} mg/m3.":
        "பச்சையம் {min_mg} முதல் {max_mg} mg/m3 வரை உள்ளது.",
    "{n} of {total} tested explanations are supported by the data.":
        "சோதிக்கப்பட்ட {total} விளக்கங்களில் {n} தரவால் உறுதிப்படுகின்றன.",
    "No tested explanation is supported: conditions here are within their normal range.":
        "சோதிக்கப்பட்ட எந்த விளக்கமும் உறுதிப்படவில்லை: இங்குள்ள நிலைமைகள் "
        "வழக்கமான வரம்பிற்குள் உள்ளன.",
    "No monthly baseline exists for this location, so it cannot be said whether "
    "chlorophyll is unusual.":
        "இந்த இடத்திற்கு மாதவாரி அடிப்படைத் தரவு இல்லை, எனவே பச்சையம் "
        "வழக்கத்திற்கு மாறானதா என்று சொல்ல முடியாது.",
    # ``month`` arrives as an integer (chl_anomaly returns 1-12), so the Tamil
    # says "the Nth month" rather than pretending to name it.
    "The {month} average here is {mean} mg/m3, and today is {sigma} standard "
    "deviations from it ({direction} normal).":
        "இங்கு {month}-ஆம் மாதத்தின் சராசரி {mean} mg/m3; இன்றைய அளவு "
        "அதிலிருந்து {sigma} நியமவிலக்கம் (வழக்கத்தை விட {direction}).",

    # --- conditions --------------------------------------------------------
    "Conditions near {place}.": "{place} அருகே கடல் நிலைமை.",

    # --- no-data answers ---------------------------------------------------
    "Could not locate fishing zones - the ocean data did not return.":
        "மீன்பிடி வலயங்களைக் கண்டறிய முடியவில்லை - கடல் தரவு வரவில்லை.",
    "Could not check maritime boundaries - the geofence data did not return.":
        "கடல் எல்லைகளைச் சரிபார்க்க முடியவில்லை - எல்லை தரவு வரவில்லை.",
    "Could not test any explanation - the chlorophyll data did not return.":
        "எந்த விளக்கத்தையும் சோதிக்க முடியவில்லை - பச்சையத் தரவு வரவில்லை.",
    "Could not read conditions - the wave forecast did not return.":
        "நிலைமையைப் படிக்க முடியவில்லை - அலை முன்னறிவிப்பு வரவில்லை.",
}

#: Appended to a driver label when a gust peak drove the breach. Kept out of
#: TEMPLATES because synthesis builds it by concatenation, not as a whole key.
_GUST_SUFFIX_EN = ", gusting {peak} {unit}"
_GUST_SUFFIX_TA = ", {peak} {unit} வரை பலத்த காற்று"


#: Closed vocabularies that arrive *inside* a slot. Each is a fixed set that
#: synthesis composes deterministically, so translating them is a lookup, not
#: a judgement call. Ordered longest-first so "high water" is consumed before
#: any shorter fragment inside it.
SLOT_PHRASES: tuple[tuple[str, str], ...] = (
    # Tide extremes, composed by _negative_findings as "; high water HH:MM".
    ("high water", "உயர் நீர்"),
    ("low water", "தாழ் நீர்"),
    # The signed side of a chlorophyll anomaly.
    ("above", "அதிகம்"),
    ("below", "குறைவு"),
    # Geofence zone codes. Names, so the acronym stays alongside the gloss --
    # a fisherman stopped by the coastguard is shown the English acronym.
    ("imbl", "சர்வதேச கடல் எல்லைக் கோடு (IMBL)"),
    ("mpa", "கடல் பாதுகாக்கப்பட்ட பகுதி (MPA)"),
    ("eez", "பிரத்யேக பொருளாதார மண்டலம் (EEZ)"),
)

#: Whole-word only. A substring replace would rewrite "above" inside a place
#: name and, worse, could land inside a number-adjacent token.
_PHRASE_RE = tuple(
    (re.compile(rf"\b{re.escape(en)}\b"), ta) for en, ta in SLOT_PHRASES
)

#: Latin letters left in the output after slot translation. Used only to
#: decide whether the honesty note is warranted -- never to alter text.
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")

#: Tokens that are Latin by design and must not trigger the note: unit
#: symbols, the treaty acronyms, and anything the gazetteer supplies as a
#: place name is handled separately by the caller's slot values.
_ALLOWED_LATIN = {
    "km", "kn", "nmi", "deg", "degT", "mg", "IMBL", "EEZ", "MPA", "IST",
}


def _translate(template: str) -> tuple[str, bool]:
    """Tamil for a template, and whether the catalogue actually had it.

    A miss returns the English pattern unchanged. That is deliberate: the
    alternative is raising, which turns a wording change in synthesis into a
    500 on a fisherman's phone. The miss is reported instead, and the parity
    test makes it unreachable in a released build.
    """
    if template in TEMPLATES:
        return TEMPLATES[template], True
    # Synthesis appends the gust suffix to a label it already chose, so a
    # gusting driver arrives as "<label>, gusting {peak} {unit}" -- a key
    # that was never authored whole.
    if template.endswith(_GUST_SUFFIX_EN):
        base = template[: -len(_GUST_SUFFIX_EN)]
        if base in TEMPLATES:
            return TEMPLATES[base] + _GUST_SUFFIX_TA, True
    return template, False


def _translate_slots(slots: dict) -> dict:
    """Translate the closed vocabularies that travel inside string slots.

    Numeric slots are returned untouched by construction -- the loop only
    looks at ``str`` -- so no digit can be rewritten here.
    """
    out = dict(slots)
    for key, value in slots.items():
        if not isinstance(value, str) or not value:
            continue
        text = value
        for pattern, tamil in _PHRASE_RE:
            text = pattern.sub(tamil, text)
        out[key] = text
    return out


class _Renderer:
    """Renders one recommendation, remembering what it could not translate.

    The bookkeeping is the point. A paragraph that is 80% Tamil and 20%
    English provenance strings should say so; guessing that the reader will
    notice is how a half-translated safety answer ships.
    """

    def __init__(self) -> None:
        self.missing_templates: list[str] = []
        self.english_prose = False

    def text(self, templated) -> str:
        """A Templated in Tamil, with its slots copied through.

        ``Driver`` is the one block that is not a ``Templated`` subclass -- it
        carries ``label_template`` -- so the attribute is resolved rather
        than assumed. Same reason the English renderer calls ``.render()``
        on both without caring which it has.
        """
        pattern = getattr(templated, "template", None) or templated.label_template
        tamil, hit = _translate(pattern)
        if not hit:
            self.missing_templates.append(pattern)
        rendered = tamil.format(**_translate_slots(templated.slots))
        self.note_residual(rendered)
        return rendered

    def note_residual(self, rendered: str) -> None:
        for word in _LATIN_WORD.findall(rendered):
            if word not in _ALLOWED_LATIN:
                self.english_prose = True
                return


def _driver_sentence(driver, renderer: _Renderer) -> str:
    """One driver, paired with the limit it is judged against.

    The ceiling/floor split is carried over from the English renderer for the
    same reason it exists there: visibility is safe while ABOVE its threshold,
    and calling 24 km "over the limit" tells a fisherman the opposite of what
    the check found. Getting that backwards in a language the crew actually
    reads is worse, not better.
    """
    from core.units import Comparison

    evaluation = driver.evaluation
    threshold = evaluation.threshold
    is_ceiling = threshold.comparison in (Comparison.LTE, Comparison.LT)
    unit = "" if threshold.unit.value in _SILENT_UNITS else f" {threshold.unit.value}"
    label = renderer.text(driver)

    observed = evaluation.observed
    peak = observed.peak
    if is_ceiling and evaluation.breaching and peak is not None and peak > observed.max:
        return (
            f"{label} - {peak:g}{unit} வரை வீசும் காற்று "
            f"{threshold.value:g}{unit} வரம்பைத் தாண்டுகிறது."
        )

    if is_ceiling:
        return (
            f"{label}, {threshold.value:g}{unit} வரம்பைத் தாண்டியுள்ளது."
            if evaluation.breaching
            else f"{label}, {threshold.value:g}{unit} வரம்புக்குள் உள்ளது."
        )
    return (
        f"{label}, {threshold.value:g}{unit} குறைந்தபட்சத்திற்குக் கீழே உள்ளது."
        if evaluation.breaching
        else f"{label}, {threshold.value:g}{unit} குறைந்தபட்சத்திற்கு மேல் உள்ளது."
    )


def render_headline(rec: Recommendation) -> str:
    if rec.refusal is not None or rec.verdict is None:
        return _Renderer().text(rec.headline)
    return _VERDICT_LINE[rec.verdict.value]


def render(rec: Recommendation) -> str:
    """The full answer in Tamil, as plain text.

    Structurally identical to the English renderer, block for block, so the
    two can be compared token by token in the parity test.
    """
    r = _Renderer()
    lines: list[str] = [render_headline(rec)]

    if rec.refusal is not None:
        if rec.refusal.supported_region:
            lines.append(f"உள்ளடக்கிய பகுதி: {rec.refusal.supported_region}")
            r.english_prose = True
        return "\n\n".join(lines)

    if rec.drivers:
        lines.append(" ".join(_driver_sentence(d, r) for d in rec.drivers))

    if rec.verdict is None and rec.claims:
        lines.append(" ".join(r.text(c) for c in rec.claims))

    for driver in rec.drivers:
        trajectory = driver.trajectory
        if trajectory and trajectory.breaches_at:
            sentence = (
                f"நிலைமை அப்படியே இருக்காது: "
                f"{trajectory.breaches_at.strftime('%H:%M')} மணிக்கு வரம்பைத் தாண்டும்."
            )
            if trajectory.peak_rate_window:
                start, end = trajectory.peak_rate_window
                sentence += (
                    f" {start.strftime('%H:%M')} முதல் {end.strftime('%H:%M')} வரை "
                    "தான் பெரும்பாலான மாற்றம் நிகழும்."
                )
            lines.append(sentence)
            break

    if rec.window is not None:
        lines.append(
            f"உங்கள் நேர இடைவெளி {rec.window.opens.strftime('%H:%M')} முதல் "
            f"{rec.window.turn_back.strftime('%H:%M')} வரை. "
            f"{rec.window.ashore_by.strftime('%H:%M')} மணிக்குக் கரை சேர "
            f"{rec.window.turn_back.strftime('%H:%M')} மணிக்குத் திரும்பத் தொடங்குங்கள்."
        )

    # Hypothesis statements are model-authored English and change every turn,
    # so only the verdict word in front of them can be Tamil. Marked as
    # residual English rather than dressed up.
    if rec.hypotheses:
        for hypothesis in sorted(rec.hypotheses, key=lambda h: not h.supported):
            mark = "உறுதிப்பட்டது:" if hypothesis.supported else "உறுதிப்படவில்லை:"
            lines.append(f"{mark} {hypothesis.render()}")
            r.english_prose = True
        lines.append(
            "தரவுக்கு எதிராகச் சோதிக்க முடியாத விளக்கங்கள் பட்டியலிடப்படாமல் "
            "நீக்கப்பட்டன."
        )

    if rec.negative_findings:
        lines.append(" ".join(r.text(f) for f in rec.negative_findings))

    for guidance in sorted(rec.operational_guidance, key=lambda g: g.priority):
        lines.append(r.text(guidance))

    for alternative in rec.alternatives:
        cost = alternative.cost
        lines.append(
            f"{r.text(alternative)} அங்கு செல்ல சுமார் "
            f"{cost.extra_distance_km:g} km கூடுதலாகப் பயணிக்க வேண்டும்."
        )

    # The honest footer. Its content is provenance text composed in English;
    # the label is Tamil so the reader knows what they are looking at.
    if rec.confidence is not None:
        lines.append(f"அடிப்படை: {rec.confidence.basis}")
        r.english_prose = True
    if rec.caveats:
        lines.append(" ".join(c.text for c in rec.caveats))
        r.english_prose = True

    if r.missing_templates or r.english_prose:
        lines.append(
            "குறிப்பு: இந்த பதிலின் சில தொழில்நுட்பப் பகுதிகள் ஆங்கிலத்திலேயே "
            "உள்ளன. எண்கள் அனைத்தும் மொழிபெயர்க்கப்படாமல் அப்படியே உள்ளன."
        )

    return "\n\n".join(lines)
