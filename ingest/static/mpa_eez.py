"""MPA / EEZ geometry check: re-runnable proof of what exists keylessly.

Checked 2026-09-07 against the UNEP-WCMC WDPA ArcGIS REST mirror
(`WDPA_poly_latest`, no auth): an envelope query over the Gulf of Mannar
(lon 78.0-79.8, lat 8.4-9.6) returns Adam's Bridge NP (LK), Delft NP (LK),
Bar Reef Marine Sanctuary (LK) and three Ramsar wetlands -- but NO Gulf of
Mannar Marine National Park polygon. Site 555558034 (sometimes cited for the
park) is absent from this snapshot.

So `gom_mpa` stays `wanted` in config/datasets.yaml and geofence_check keeps
reporting `mpa` as unavailable. Committing a hand-drawn polygon would violate
the do-not-fake-data rule; this script exists so the check can be re-run
whenever WDPA updates rather than re-argued from memory.

EEZ: inside the South Coromandel box the operative line IS the IMBL -- Article
5(2) of the 1976 India-Sri Lanka agreement designates the boundary as the EEZ
delimiter, and the treaty turning points are already digitised in
ingest/static/boundaries.py. A Marine Regions v12 polygon remains `wanted`
for completeness (keyless download recorded in datasets.yaml).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

__all__ = ["GOM_ENVELOPE", "wdpa_sites_in_envelope", "main"]

WDPA_URL = (
    "https://data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/"
    "The_World_Database_of_Protected_Areas/MapServer/1/query"
)

#: Gulf of Mannar envelope, EPSG:4326.
GOM_ENVELOPE = {"xmin": 78.0, "ymin": 8.4, "xmax": 79.8, "ymax": 9.6}

_UA = "orca/0.1 (+python-urllib)"


def wdpa_sites_in_envelope() -> list[dict]:
    """Protected sites intersecting the envelope, as attribute dicts."""
    params = {
        "geometry": json.dumps({
            **GOM_ENVELOPE,
            "spatialReference": {"wkid": 4326},
        }),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "site_id,name,desig_eng,gis_m_area",
        "returnGeometry": "false",
        "f": "json",
    }
    url = WDPA_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [f["attributes"] for f in payload.get("features", [])]


def main() -> int:
    sites = wdpa_sites_in_envelope()
    print(f"{len(sites)} protected sites intersect the GoM envelope:")
    gom_mnp = False
    for site in sites:
        name = str(site.get("name") or "")
        print(f"  {site.get('site_id')} | {name[:60]} | {site.get('desig_eng')}")
        if "mannar" in name.lower() and "marine national park" in str(site.get("desig_eng") or "").lower():
            gom_mnp = True
    if not gom_mnp:
        print("Gulf of Mannar Marine National Park: NOT in WDPA mirror -> stays wanted.")
        return 1
    print("GoM Marine National Park present; geometry fetch is the next step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
