"""Corpus builder: explainer passages from in-repo cited text only.

Every document comes from words the repo already stands behind -- threshold
notes with their provenance tags, treaty transcription notes, dataset gap
notes, the climatology sensor-choice reasoning. No web fetch, no model output,
no live numbers. Bodies WILL contain figures like "2.5 m"; that is expected,
because strip_numbers() in rag/store.py removes them at retrieval time, in
code, before any caller sees them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Doc", "build_corpus"]


@dataclass(frozen=True)
class Doc:
    source: str
    title: str
    body: str


def _threshold_docs() -> list[Doc]:
    """One doc per `note:` field in config/risk_thresholds.yaml, plus the header essay."""
    from core import config

    raw_text = (config.CONFIG_DIR / "risk_thresholds.yaml").read_text(encoding="utf-8")
    data = config.load_yaml("risk_thresholds.yaml")
    docs: list[Doc] = []

    header = "\n".join(
        line[2:] for line in raw_text.splitlines()[:80] if line.startswith("# ")
    ).strip()
    if header:
        docs.append(Doc(
            source="risk_thresholds.yaml#header",
            title="How ORCA thresholds are set and cited",
            body=header[:2000],
        ))

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            note = node.get("note")
            if isinstance(note, str) and note.strip():
                provenance = str(node.get("provenance", "unstated"))
                docs.append(Doc(
                    source=f"risk_thresholds.yaml#{path or 'root'}",
                    title=f"Threshold note ({provenance}): {path or 'general'}",
                    body=note.strip()[:1500],
                ))
            for key, value in node.items():
                if key != "note":
                    walk(value, f"{path}.{key}" if path else str(key))

    walk(data, "")
    return docs


def _treaty_docs() -> list[Doc]:
    from ingest.static.boundaries import GEOMETRY_NOTES, TRANSCRIPTION_NOTES

    return [
        Doc(
            source="ingest/static/boundaries.py#transcription",
            title="Treaty transcription corrections",
            body=" ".join(TRANSCRIPTION_NOTES)[:1500],
        ),
        Doc(
            source="ingest/static/boundaries.py#geometry",
            title="Boundary geometry approximations",
            body=" ".join(GEOMETRY_NOTES)[:1500],
        ),
    ]


def _dataset_docs() -> list[Doc]:
    from ingest.catalogue import resolve_datasets

    docs: list[Doc] = []
    for record in resolve_datasets():
        note = str(record.get("note") or "").strip()
        if not note:
            continue
        docs.append(Doc(
            source=f"config/datasets.yaml#{record.get('id')}",
            title=f"Dataset note: {record.get('id')} ({record.get('status')})",
            body=note[:800],
        ))
    return docs


def _climatology_doc() -> Doc:
    from ingest import climatology

    return Doc(
        source="ingest/climatology.py#sensor-choice",
        title="Why the chlorophyll baseline is VIIRS, not MODIS",
        body=str(climatology.__doc__ or "")[:2000],
    )


def build_corpus() -> list[Doc]:
    """All explainer passages. Deterministic; no network, no model."""
    docs = _threshold_docs() + _treaty_docs() + _dataset_docs() + [_climatology_doc()]
    return [d for d in docs if d.title.strip() and d.body.strip()]
