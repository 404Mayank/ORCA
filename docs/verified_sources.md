# Verified source facts

Every external fact ORCA relies on, with how it was checked and when. If a
number is not in this file, it has not been verified and must not be presented
as though it were.

Three statuses:

| Status | Meaning |
|---|---|
| **verified** | Checked directly, by calling the API or reading the publisher's own page. Reproduction steps given. |
| **reported** | Found in a secondary source and not independently confirmed. Usable, but attributed as second-hand. |
| **unverified** | We could not obtain it. Recorded so the gap stays visible. Never cite as fact. |

Last full pass: **2026-09-04**.

---

## Open-Meteo Marine API — verified

**Endpoint:** `https://marine-api.open-meteo.com/v1/marine`
**Auth:** none. No key, no registration.
**Checked by:** live request for 10.77 N, 79.84 E (Nagapattinam), 2026-09-04.

All eleven variables we intend to use returned 48/48 non-null hours:

| Variable | Unit returned | Note |
|---|---|---|
| `wave_height` | m | significant wave height |
| `wave_direction` | ° | |
| `wave_period` | s | |
| `wind_wave_height` | m | |
| `wind_wave_period` | s | |
| `swell_wave_height` | m | |
| `swell_wave_period` | s | |
| `sea_surface_temperature` | °C | |
| `ocean_current_velocity` | **km/h** | see unit trap below |
| `ocean_current_direction` | ° | |
| `sea_level_height_msl` | m | our tide signal |

### `sea_level_height_msl` exists — a secondary source said otherwise

One search summary claimed this variable is not available on the Marine API.
It is. The live request returned 48 non-null hourly values in metres. The
secondary source was wrong; the direct call settled it. This is the reason the
file exists.

### Unit trap: ocean currents are km/h

`ocean_current_velocity` comes back in **km/h**, not m/s and not knots. Wave
and wind fields elsewhere in the project are in m and knots. Convert explicitly
at the ingest boundary. `core.units.evaluate()` raises on a unit mismatch
rather than converting, which is what turns this from a silent wrong answer
into a loud failure.

### Native resolution is 1/12°, NOT 0.05° — measured

Requesting 10.77 N, 79.84 E returns data stamped **10.791664 N, 79.95836 E**.
That is a grid snap, so the model grid was measured directly with a longitude
sweep at fixed latitude:

| Requested lon | Returned cell centre |
|---|---|
| 79.80 → 80.00 | 79.95836 |
| 80.04, 80.08 | 80.04167 |

Spacing **0.08331° = 1/12.003**, i.e. a **1/12° (~9.3 km)** grid for the Bay of
Bengal. Open-Meteo's own docs quote 0.05° only for DWD EWAM over Europe, which
does not cover us.

**This corrected an error in `config/bbox.yaml`,** which had claimed 0.05° was
chosen because wave data lands on it without interpolation. It does not.
Regridding waves to 0.05° is upsampling and creates no new information. The
0.05° grid is a common frame for joining datasets, which is worth having — but
it is not a resolution claim, and `native_resolution_deg` must carry 0.0833 for
Open-Meteo wave data.

CLAUDE.md lists INCOIS MWW3 at 0.05° as an upgrade path. That figure is
**unverified** and must not be repeated until it is.

**Reproduce:**
```bash
curl -s "https://marine-api.open-meteo.com/v1/marine?latitude=10.77&longitude=79.84&hourly=wave_height,sea_level_height_msl&forecast_days=1&timezone=UTC" | head -c 400
```

---

## Open-Meteo Forecast API (wind) — verified

**Endpoint:** `https://api.open-meteo.com/v1/forecast`
**Auth:** none.
**Checked:** 2026-09-04, same point.

- `wind_speed_10m`, `wind_gusts_10m`, `wind_direction_10m`, `visibility`,
  `precipitation` all return.
- `wind_speed_unit=kn` is honoured — the API returns knots directly, so we do
  not hand-convert. Confirmed by the response's own `hourly_units`.
- **`visibility` is returned in metres**, not km. Our threshold in
  `risk_thresholds.yaml` is in km. Convert at the boundary.
- Different grid from the marine endpoint: the same request snapped to
  10.790861 N, 79.81432 E, which is not a 1/12° centre. Wind and wave
  therefore come from **different grids** and are co-located only after
  regridding. Not yet measured precisely; recorded as a known gap.

---

## Open-Meteo Archive API (Cyclone Gaja replay) — verified

**Endpoint:** `https://archive-api.open-meteo.com/v1/archive`
**Auth:** none. Backed by ERA5.
**Checked:** 2026-09-04, request for 10.77 N, 79.84 E, 15–17 Nov 2018.

72 hourly records returned. Peak wind gust **68.6 kn at 2018-11-16 02:00 IST**,
minimum mean sea level pressure **999.6 hPa**.

### The replay works, with one honest caveat

A 68.6 kn gust is a genuine, dramatic signal — more than double the 25 kn limit
for a 9 m FRP boat — so the demo premise holds: the risk score does go hard red
around landfall, from real archived data.

But **999.6 hPa is not Gaja's true central pressure.** Gaja was a severe
cyclonic storm and its core was far deeper than that. ERA5 is a ~0.25°
reanalysis and smooths a tropical cyclone core heavily; the value is what ERA5
holds at this grid cell, not what a barometer at Nagapattinam read.

So the replay must be presented as **"ERA5 reanalysis of the Gaja period"**,
never as "what Gaja actually did". If a judge asks why the pressure looks
shallow, the answer is reanalysis smoothing — and knowing that in advance is
better than being caught by it. The wind field is the useful signal here; the
pressure field is not, and we should not plot it as though it were.

---

## Groq model roster — verified

**Endpoint:** `https://api.groq.com/openai/v1/models`
**Checked by:** live request with the project key, 2026-09-04.

**The models CLAUDE.md names for the fast tier are not offered.** The brief
says "Llama 3.3 70B / Mistral Large via Groq". Neither appears in the roster
returned for our key. What is available, text models only:

| Model | Context |
|---|---|
| `openai/gpt-oss-120b` | 131,072 |
| `openai/gpt-oss-20b` | 131,072 |
| `qwen/qwen3.6-27b` | 131,072 |
| `qwen/qwen3.8-27b` | 131,042 |
| `groq/compound`, `groq/compound-mini` | 131,072 |

`config/models.yaml` uses `openai/gpt-oss-120b` for both roles. Rosters
change; **re-list before a demo**, because an unlisted model id fails with a
404 that the client reports as a quiet fall-through to the next tier rather
than a crash.

### Cloudflare rejects `urllib`'s default User-Agent

The first call returned HTTP 403 with Cloudflare error code 1010. The key
was fine. Sending a real `User-Agent` header fixed it. Recorded because it
looks exactly like a bad key and is not one.

**Reproduce:**
```bash
curl -s -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models | head -c 600
```

---

## INCOIS — partly verified

See the header of `config/risk_thresholds.yaml` for the full treatment.

**Verified** (publisher's own pages, 2026-09-04):
- High Wave Alert: significant wave height 3.0–3.5 m → alert; > 3.5 m →
  warning. Swell height 2.5–3.0 m → alert; > 3.0 m → warning.
- Small Vessel Advisory Service covers vessels of **beam width up to 7 m**,
  stated to span the full range of Indian fishing vessel beam widths across all
  nine coastal states and UTs.
- SVAS warnings are driven by a **Boat Safety Index** computed from significant
  wave height, wave steepness, directional spread, and rapid wind-sea
  development, three days ahead.

**Unverified:**
- The BSI formula and its warning threshold. The defining paper returns HTTP
  403 and the SVAS portal refused connections from our network. **We do not
  implement BSI and do not claim to** — pinned by
  `tests/test_thresholds.py::test_we_do_not_claim_to_implement_bsi`.
- Per-vessel-class operating limits. INCOIS publishes coastline-segment alerts,
  not per-boat limits. Every per-class number in our config is therefore ours,
  tagged `orca` or `provisional`.
- INCOIS MWW3 wave resolution of 0.05°. Repeated from the project brief, never
  confirmed.
- ERDDAP dataset ids listed in CLAUDE.md. Not yet queried.

---

## Derived formulas — verified as textbook

**Deep-water wave steepness**, used in `risk_thresholds.yaml`:

```
S = 2*pi*Hs / (g*Tp^2)
```

From the linear (Airy) deep-water dispersion relation `L = g*T^2/(2*pi)`, so
`S = Hs/L`. Standard result, needs no attribution beyond linear wave theory,
and is **not** an INCOIS-specific quantity. Both inputs are available from the
Marine API (`wave_height`, `wave_period`), so this is computable today.

---

## Satellite ocean colour and SST — verified live 2026-09-06

**The Copernicus Marine blocker is resolved without a Copernicus account.**

CLAUDE.md calls Copernicus L4 *required*, on the correct reasoning that monsoon
Coromandel is overcast for days and an L2 product will be full of holes. The
requirement is for **gap-filled L4 data**, not for Copernicus specifically.
Three NOAA CoastWatch ERDDAP datasets meet it, keyless.

| Dataset | Role | Resolution | Coverage verified |
|---|---|---|---|
| `jplMURSST41` | near-real-time SST | **0.01°** | to 2026-09-04, 27.30–30.22 °C over the box |
| `nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily` | near-real-time chlorophyll, DINEOF gap-filled | 1/12° | to 2026-09-03, 0.066–57.49 mg/m³ |
| `nesdisVHNSQchlaMonthly` | monthly baseline for anomalies | 0.0375° | 2012-01 to 2026-07, global |

MUR is *finer* than the 0.05° Copernicus product the brief assumed, and
gap-free by construction — it is an analysis, not a swath composite.

Reproduce:

```
https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.json?analysed_sst[(2026-09-04T09:00:00Z)][(8.0):5:(12.0)][(78.5):5:(82.0)]
```

### INCOIS ERDDAP — evaluated and rejected as a live source

`erddap.incois.gov.in` responds and serves 17 datasets, but **every gridded
ocean-colour and SST product on it is a historical archive**:

| Dataset | Last data |
|---|---|
| `incois_oceansat2_datasets` (chl-a, KD490, TSM) | **2020-05-01** |
| `incois_tmi_3day_datasets` (SST + wind) | **2014-12-31** |
| `NOAA_AVHRR_AMSR_datasets` | 2011-10-04 |
| `IRS_chlorophyll_datasets` | 2006-03-21 |
| `ascat_daily_datasets` | 2023-05-21 |

Only the ARGO products are current (to 2026-07). CLAUDE.md lists the Oceansat-2
and TMI ids as live sources; **they are not**, and the entry should be read as
an archive reference. Still useful for long-record work, useless for "what is
happening today".

Two traps recorded so nobody rediscovers them:

- **`erddap.incois.gov.in` serves an incomplete certificate chain** (missing
  intermediate). `certifi` does not fix it. Verification is disabled for that
  host only, in `ingest/sources/erddap.py`, for public unauthenticated reads.
- **A CoastWatch dataset with a global-sounding name may be regional.**
  `erdVHNchlamday` is North Pacific only (lon −180 to −110); `erdMBchlamday` is
  Pacific (lon 120 to 320). Both return HTTP 404 *"your query produced no
  matching results"* for the Bay of Bengal, which reads exactly like a
  malformed request. Check `geospatial_lon_min` before assuming global.

### Descending latitude axes

`nesdisVHN*` products run latitude **north → south**. An ERDDAP griddap
selector written low-to-high on a descending axis returns an **empty grid, not
an error** — indistinguishable from "no data today". `GridSlice` handles this
per-dataset via `lat_ascending`.

---

## Measured: the Bay is thermally flat in early September

Computed from `jplMURSST41` over the box on 2026-09-04, central differences at
0.05°:

```
n = 3437 cells    min 0.0002    median 0.0200    p75 0.0326
                  p95 0.0567    max  0.1004  °C/km
```

`ThermalFrontIn.min_gradient_deg_c_per_km` defaults to **0.2**, which finds
**zero fronts** on this data. That default is not wrong for the northeast
monsoon; it is simply out of season. The inter-monsoon lull produces a nearly
uniform surface layer, and reporting "no significant thermal fronts today" is
the correct answer rather than a bug.

`pfz_candidates` therefore ranks relative (upper quartile) but gates absolute
(`MIN_USEFUL_GRADIENT = 0.05 °C/km`, ≈ 0.5 °C across 10 km, the conventional
PFZ front). Without the absolute floor the tool would rank the flattest day of
the year and present the winner as a fishing zone.

**Consequence for the demo:** a live September run legitimately returns few or
no PFZ candidates. The Gaja replay, and any northeast-monsoon date, is where
this tool shows what it can do.

---

## Chlorophyll anomalies must be compared disc to disc

Found by inspecting output, 2026-09-06. Comparing a 25 km observed mean against
the **single nearest** climatology pixel produced **−8.15 σ** at Point Calimere
— the nearest baseline pixel there is turbid estuarine water averaging
52 mg/m³, while the disc around it is mostly cleaner offshore water.

Aggregating the baseline over the same disc as the observation
(`MonthlyClimatology.mean_within_km`) brought every reference point into a
plausible −0.13 to +0.88 σ band. The pooled standard deviation combines each
pixel's interannual variance with the spatial variance between pixels, because
both are real spread in "what does September normally look like here".

Chlorophyll is log-normally distributed, so σ is computed on a log scale:

```
sigma_ln^2 = ln(1 + (s/m)^2)
mu_ln      = ln(m) - sigma_ln^2 / 2
sigma      = (ln(x) - mu_ln) / sigma_ln
```

Raw-scale σ understates a low anomaly and overstates a high one.

---

## Still to verify

- [x] INCOIS ERDDAP dataset ids and their actual resolutions — done
      2026-09-06. All gridded ocean products are archives; see above.
- [x] Copernicus Marine L4 SST/chlorophyll — **no longer needed.** NOAA
      CoastWatch MUR SST and gap-filled VIIRS chlorophyll meet the L4
      requirement keylessly. Copernicus remains a possible upgrade, not a
      blocker.
- [ ] A northeast-monsoon date to exercise thermal_front at its default
      0.2 °C/km threshold; September gradients top out at 0.10.
- [ ] Marine Regions MRGID 8480 for the India–Sri Lanka IMBL.
- [ ] Limits in the Seas No. 77 turning-point coordinates, to digitise from
      the treaty text.
- [ ] Open-Meteo forecast-endpoint grid spacing, measured the same way as the
      marine one.
- [ ] Vessel cruise speeds and beam widths — currently `provisional`, and they
      now drive the computed `turn_back`.
