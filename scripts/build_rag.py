"""Upsert the explainer corpus into Supabase. Fail-closed by design.

    python scripts/build_rag.py            # upsert all docs
    python scripts/build_rag.py --dry-run  # print docs, touch nothing

Without SUPABASE_URL and a key this prints a skip notice and exits 0: an
unconfigured store is a supported state (retrieval returns [] and answers are
identical), not an error. Apply db/migrations/001_rag.sql in the Supabase SQL
editor before the first real run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from rag.corpus import build_corpus


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    docs = build_corpus()
    print(f"[rag] {len(docs)} explainer docs built from in-repo cited text.")

    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip() or os.environ.get(
        "SUPABASE_ANON_KEY", ""
    ).strip()
    if dry:
        for doc in docs:
            print(f"  - [{doc.source}] {doc.title}")
        return 0
    if not url or not key:
        print("[rag] skipped: SUPABASE_URL and a key are not set. Nothing upserted.")
        return 0

    rows = [{"source": d.source, "title": d.title, "body": d.body} for d in docs]
    try:
        response = httpx.post(
            f"{url}/rest/v1/rag_docs",
            params={"on_conflict": "source,title"},
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Prefer": "resolution=merge-duplicates",
            },
            json=rows,
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001 -- reported, the store stays as it was
        print(f"[rag] upsert failed ({type(exc).__name__}: {exc}); store unchanged.")
        return 1
    if response.status_code not in (200, 201):
        print(f"[rag] upsert rejected: HTTP {response.status_code}: {response.text[:200]}")
        return 1
    print(f"[rag] upserted {len(rows)} docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
