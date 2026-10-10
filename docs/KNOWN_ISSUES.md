# DYOTAK — Known Issues & Unverified Parameters

Per ARCHITECTURE.md Section 16, outcomes of G0 data-access verification are
recorded here. "Verified" means checked against live services on 2026-10-08
(re-checked 2026-10-09 for the ohsome v2 extraction, and 2026-10-10 for the
CDSE Process API clips, the S1 pre/post log-ratio and the S2 SCL clip).

## Verified live

| Item | Endpoint / value | Result |
|---|---|---|
| CDSE Sentinel-1 GRD catalog | `POST https://stac.dataspace.copernicus.eu/v1/search`, collection `sentinel-1-grd` | 7 IW scenes for the Trishuli window; `sat:relative_orbit`, `sat:orbit_state`, `datetime`, `id` returned |
| CDSE Sentinel-2 L2A catalog | same endpoint, collection `sentinel-2-l2a` | 24 scenes; `eo:cloud_cover` returned |
| Same-orbit pairing | `app/pipeline/pairing.py` on live catalog output | orbit 19, DESCENDING, 2 pre scenes |
| Copernicus DEM 30m | `https://copernicus-dem-30m.s3.amazonaws.com/<tile>/<tile>.tif` | 2 tiles fetched, 3600x3600 float32 GeoTIFF, pixel scale 1 arc-second, tiepoints 85E 28N / 85E 29N |
| ohsome v2 extraction | `POST https://api.heigit.org/ohsome-api/v2-rc/extraction/features.parquet` (with `DYOTAK_OHSOME_API_KEY`) | 200 GeoParquet for the Trishuli AOI; per-type counts on 2026-08-23: building 60024, highway 3565, bridge 102, amenity 6, place 93 |
| OAuth token URL | `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token` | Token exchange exercised live with client credentials; 400/401/403 raise `CdseAuthError` without retrying |
| CDSE Process API (S1) | `POST https://sh.dataspace.copernicus.eu/api/v1/process`, type `sentinel-1-grd`, `polarization=DV`, `orthorectify=true`, `demInstance=COPERNICUS_30`, `backCoeff=GAMMA0_ELLIPSOID` | 200 GeoTIFF, 7 055 122 bytes, 983x1113 @20 m, 3 bands (VV, VH, dataMask) already in dB, bounds 85.15/27.85/85.35/28.05; per-band stats in the resolved finding below |
| CDSE Process API (S2) | same endpoint, type `sentinel-2-l2a`, SCL evalscript | 200 GeoTIFF SCL clip; live class histogram (classes 2-9), 100 % valid, cloud 32.78 % against 54.29 % catalog |
| S1 pre/post pair | post 2026-08-28 / pre 2026-08-16 (orbit 85, ASCENDING) clipped and compared | Identical grid (both 1113x983, same bounds); log-ratio median -0.21 dB (VV) / -0.15 dB (VH), i.e. one shared dB scale |

## Resolved findings (measured 2026-10-10)

### S1 backscatter units: the "-60 dB floor" and the "~+25 dB maximum" were artefacts

The earlier G0 run reported `min=-60.0 dB` and `VV max=+25.5 dB` for the
Trishuli clip and flagged the range as suspect for the expected ~-40..+10 dB
band. Both ends are now explained by measurement on live clips (20 m,
983x1113, post 2026-08-28 / pre 2026-08-16, same orbit 85 ASCENDING):

- **The -60 dB is the evalscript's own clamp floor**, `10*log10(max(linear,
  1e-6))`, hit by pixels with no backscatter. The evalscript now also returns
  `dataMask`, and the spike treats `mask == 0`, non-finite and floor pixels as
  nodata, so those pixels no longer reach the statistics: measured **nodata
  0.00 % and floor 0.00 %** in both polarizations, with `p1` = -16.6 dB (VV) /
  -22.7 dB (VH) instead of the old -60 dB.
- **The +25.5 dB was a maximum over all pixels** — a handful of very bright
  targets, not a scaling error. Measured share of valid pixels above +10 dB:
  **0.04 % (VV), 0.00 % (VH)**. The bulk distribution is inside the plausible
  band: `p1/p50/p99` = -16.6/-6.6/+6.0 dB (VV), -22.7/-13.0/-1.2 dB (VH).
- **Both clips share one dB scale**, which is what the damage stage relies on:
  the post-pre log-ratio median is **-0.21 dB (VV) / -0.15 dB (VH)** on identical
  grids, so absolute-dB questions no longer block the delta use.

### S2 SCL clip: why every scene produced the same 8754-byte file

The STAC `datetime` is the **datatake start**, and the AOI is sensed ~15-25 min
later, so the old 60-second window returned an empty mosaic (all class-0 SCL,
which deflate compresses to the same 8754 bytes) and the cloud estimate reported
0.0 %. The clip window now spans the whole datatake
(`fetch.s2_datatake_window_seconds`; measured: +60 s and +10 min returned empty
mosaics, +30 min returned data), the spike prints the SCL class histogram and the
valid fraction, and an all-nodata clip returns `cloud_pct=None` plus a warning
instead of 0.0. Live (scene selected by the same rule as the catalog row):
`S2B_MSIL2A_20260827T045659..._T45RUL`, 2026-08-27, 100 % valid,
`hist[0..11]` = [0:0 1:0 2:426 3:17441 4:155934 5:8069 6:477 7:1860 8:28940
9:60897 10:0 11:0], **cloud 32.78 %** vs 54.29 % catalog.

## Stages 6 & 7 (damage + isolation) — measured 2026-10-10

[...]
2. **Radiometric terrain correction (RTC).** The clip is orthorectified
   (`orthorectify=true`, `demInstance="COPERNICUS_30"`) but not terrain-flattened,
   so slope-dependent radiometric bias remains in the steep Trishuli valley and
   has not been quantified.
3. **Flood threshold from the log-ratio tail.** The measured share of pixels
   below -3 dB (post minus pre, 12-day gap) is 12.26 % (VV) / 12.49 % (VH) with
   `p1` = -6.06 dB (VV) / -6.27 dB (VH). No independent flood mask exists yet to
   separate flood from soil-moisture and vegetation change, so this tail is
   reported, not thresholded.
4. **Process API path.** CDSE announced (2026-03-09) a new path format
   `/process/v1` alongside legacy `/api/v1/process`. This code uses the legacy
   path `https://sh.dataspace.copernicus.eu/api/v1/process` (both are stated to
   work, and the legacy path answered 200 today). Switch to `/process/v1` before
   the legacy path is deprecated.
5. **S1 clip quality WARN branch.** The spike downgrades the clip row to WARN
   above 5 % nodata / 1 % non-finite; both thresholds are unit-tested but have
   not been observed live, because the Trishuli clip measures 0.00 % nodata and
   0.00 % non-finite in both polarizations.
6. **ohsome extraction (v2 verified; endpoint still `-rc`).**
   - v1 `POST https://api.ohsome.org/v1/elements/geometry` returns **HTTP 403**
     (Apache "Forbidden") for anonymous requests, while `elements/count` and
     `/v1/metadata` return 200. The v1 extraction endpoint appears disabled and
     v1 shuts down 2026-11-30; the client uses v2.
   - v2 requires a free API key (`DYOTAK_OHSOME_API_KEY`); it returns **HTTP 401**
     without a key and 403 with an invalid key. With a valid key the extraction
     returns **200 GeoParquet** (verified 2026-10-09).
   - **v2 time format (verified live).** `time.start`/`time.end` must be full
     timezone-aware ISO-8601 UTC timestamps, and v2 requires **end > start**:
     - bare date `"2024-09-25"` -> **422** ("Input should be a valid datetime"),
     - `start == end` -> **422**
       ("End timestamp needs to be greater than start timestamp"),
     - `end = start + 1 day` -> **200**.
     The client therefore sends a point window (`start == end`) first and, on
     422, retries once with `end = start + 1 day` — only when that day still
     precedes `event_date` (Rule 5). The form that succeeded is written to the
     result's `time_window.form` provenance (`day_range` in practice) and is
     shown by the G0 spike.
   - The spike also prints the **real data timestamps**: the newest
     `edit_timestamp` in the returned extract (measured 2026-08-13T11:58:42Z,
     before the 2026-08-26 event) and the ohsome instance's latest snapshot from
     `GET /v2-rc/metadata` (2026-10-09T07:25:28Z — after the event, since the
     instance is live). "Pre-event" is therefore asserted on the extract's own
     data timestamp, not on the instance coverage end.
   - v2 returns **GeoParquet**; feature counts are read with `pyarrow`
     (`count_parquet_features`). `pyarrow` is declared in `requirements.txt`;
     without it counts degrade to `-1`.
   - HTTP 401/403/422 and other 4xx are never retried; only timeouts, connection
     errors and 5xx are. Error messages carry the full (untruncated) response
     body and are not nested inside themselves.
   - The `-rc` URL and the Overpass Attic fallback remain untested against a
     truly unavailable ohsome instance.

## Baseline flood map (G1) — measured 2026-10-10

`app/pipeline/flood_baseline.py` is the honest baseline: log-ratio change
detection with speckle filtering, permanent-water, slope, radar shadow/layover
and HAND exclusions. No model, no calibration against EMSR927 or any published
flood map. Live Trishuli run (post 2026-08-28, orbit 85 ASCENDING, median of the
2 same-orbit pre scenes, 983x1113 @20 m, UTM 45N):

| Step | Pixels |
|---|---|
| grid | 1 094 079 |
| valid pre/post overlap | 1 094 063 |
| candidate (probability >= 0.50) | 183 055 |
| excluded: permanent water (pre-event, both polarizations) | 446 |
| excluded: slope > 15 deg | 148 694 |
| excluded: radar shadow/layover | 0 |
| excluded: HAND > 15 m | 12 832 |
| excluded: components smaller than 9 px | 5 743 |
| **final mask** | **15 340 -> 6.13 km2, 792 polygons** |

- Otsu chose a split above the configured fallback range, so the threshold was
  clamped to `flood.otsu_fallback_max_db` (-1.0 dB). The fallback range is doing
  real work on this AOI, and the pre-exclusion candidate mask is generous
  (16.7 % of the AOI): the terrain masks carry most of the discrimination.
- The radar shadow/layover mask excluded **0** pixels here: no pixel in this
  window has a single-sided range slope beyond 52 deg. It is kept as a guard for
  other AOIs (steep terrain is otherwise excluded by the slope cutoff).
- The probability raster is a monotone ranking, not a calibrated probability,
  and the flood area is a threshold-and-geometry product.
- Still unverified for this stage: mask accuracy against an independent
  reference (no post-event reference is read inside `/app`), the sensitivity of
  the Otsu fallback bounds, the effect of the missing sink filling in the HAND
  proxy, and radiometric terrain correction (see the S1 items above).

## G0 hardening (applied)

- **DEM range reads.** `dem.read_dem_window` opens the remote COG via GDAL
  `/vsicurl/` and reads only the AOI window. Measured on the Trishuli bbox
  (2 tiles): **720x720 px in ~4.4 s**, values 488..3256 m, versus **~195 s** to
  download the full tiles. The extracted window is cached as a small GeoTIFF.
  `fetch_dem_tile` (full download) is retained as a fallback.
- **Fail-fast auth.** `CdseAuthError` / `OhsomeAuthError` derive from
  `NonRetryableError`; the retry wrapper re-raises them without retrying. HTTP
  401/403 from the token, STAC, Process, and ohsome endpoints, and missing
  credentials/API keys, now fail immediately.
- **Spike assertions.** `s1_pairing_report` asserts the post scene is the first
  acquisition on/after the event and the pre/post gap matches the Sentinel-1
  repeat cycle (config `pairing.primary_repeat_days`, ±2 days), and prints both
  dates. Live: post=2024-10-02, pre=2024-09-20, gap=12 d.
- **Overpass fallback.** `ohsome.build_overpass_attic_query` builds a single
  Overpass QL query using the `[date:"…"]` attic setting, to swap in if ohsome
  is unavailable during judging. See the module docstring for caveats.

## Stages 6 & 7 (damage + isolation) — measured 2026-10-10

Live Trishuli run: cached ohsome v2 extracts (Stage 0, 0.15 s, not in the
compute budget), same-orbit S1 baseline flood map (Stages 1-4, ~19.7 s, clip
downloads dominate), then Stages 6 and 7 over the shared OSM layers. The
60 024 building footprints are sampled in **0.30 s**; the two slower pieces are
the road flood-probability computation and the NetworkX graph build.

| Stage | call | wall s | notes |
|---|---|---|---|
| 6 | `classify_damage` (buildings + roads + bridges) | **6.48** | buildings_s 0.30, roads_s 5.46, bridges_s 0.24, read_parquet 0.43 |
| 7 | `compute_isolation` (over the damage result, so the road layer is shared) | **6.10** | graph_s 5.84, read_parquet 0.13, snap 0.01, solve 0.10 |
| **6+7 combined** | — | **12.58** | over the live AOI; budget was <10 s | |

### 6.1 Buildings (Stage 6) — under budget, vectorised

- 60 024 preevent buildings sampled in **0.30 s** (one footprint sampled per
  ~0.005 ms). The sampling is vectorised: the clipping and raster lookup run as
  numpy array operations over all footprints, not one Python loop per building.
- Status and confidence are derived from the flooded **fraction** of each
  footprint against `damage.affected_overlap_fraction` / `possibly_affected` and
  the confidence margins. Pre-event buildings only (no post-event OSM read inside
  `/app`).
- Result: 1 219 affected, 0 possibly affected, 58 805 unaffected.

### 6.2 Roads (Stage 6) — hotspot, shapely + rasterio per sample

- roads_s = **5.46 s** for 3 565 highway ways. The work is real, not a
  profiling artefact: `sample_lines_at_spacing` subsamples each way to the
  configured 20 m spacing (shapely `interpolate`), then samples the flood raster
  at each point (rasterio `transform` + env enter/exit per call).
- The dominant cost is **shapely `interpolate` + rasterio transform** per
  sample point. In the profiler, `shapely.convert.lines` (the `sample_lines`
  path), `shapely.wkb.loads`/`shapely.wkt.loads` (the `sample_lines_at_spacing`
  fallback), and `rasterio.transform` are the top lines.
- Output: 1 138 road segments affected (31.06 km), plus possibly_affected and
  unaffected. Road damage is reported per segment, not per way.

### 6.3 Bridges (Stage 6) — cheap, possibly_impacted only

- bridges_s = **0.24 s** for 102 bridges (99 road, 3 non-road). Only
  `possibly_impacted` is ever emitted — bridges are never labelled `destroyed`
  (ARCHITECTURE 4.6).
- Result: 28 possibly impacted, 74 unaffected.

### 7.1 Road graph (Stage 7) — hotspot, same shapely + rasterio root cause

- `graph_s` = **5.84 s**, almost identical to damage.roads_s. The same shapely
  interpolation + rasterio transform pattern is used to build the edge flood
  probability, so the hotspot is shared.
- `contract_degree_two` runs to a stable fixed point (repeated until no node or
  edge count changes). Junction-free cycles (ring roads) are **kept**, not
  dropped: dropping them would delete real road and would make settlements on the
  loop unsnappable, i.e. a false `no_pre_event_access`. On the live run 0 cycle
  segments were kept (no ring roads in this window).
- Parallel chains between the same two junctions are **merged** (lengths, flooded
  length, max and length-weighted flood probability added), never overwritten.
- Live graph: 3 565 edges contracted to a junction/endpoint graph of
  **631 nodes and 1 009 edges**, total road length 1 808.26 km.

### 7.2 Solve (Stage 7) — fast

- `snap_s` 0.01 s, `solve_s` 0.10 s. `nearest_nodes` uses a chunked brute-force
  nearest-neighbour search (no scipy), with points beyond the snap distance
  reporting **distance = inf** (so a report never misreads a large distance as a
  snap).
- Severance rule: an edge is removed when its **length-weighted mean** flood
  probability is at or above `isolation.flood_threshold_edge` (a mean, not a
  maximum — a long road clipping one flooded pixel is not severed).
- `multi_source_dijkstra_path_length` is run from the destinations in the intact
  pre-event graph and in the post-event graph with severed edges removed.
- Live: 47 edges severed (6.17 km), 5 of 6 destinations snapped, 1 settlement
  cut off (1), 59 still connected, 2 with no pre-event access.

### 7.3 Edge cases tested

Each of the ARCHITECTURE edge cases has a dedicated test in
`tests/pipeline/test_isolation.py`, all green:

- disconnected pre-event graph -> `no_pre_event_access`, no flood attribution;
- no facilities / no road graph -> typed `NO_DESTINATIONS` / `NO_ROAD_GRAPH`,
  nothing guessed;
- all edges flooded -> every connected settlement newly cut off;
- a settlement far from any road -> not snapped, `no_pre_event_access`;
- a bridge over a flooded river -> far settlement cut off;
- with a detour around the bridge -> far settlement still connected at a
  positive extra distance.

### 7.4 Optimisation direction (not applied yet)

- The shared shapely + rasterio per-sample hotspot is the one thing to fix if the
  <10 s budget becomes a hard requirement. Two concrete options, in order of
  expected yield:
  1. replace `sample_lines_at_spacing` + per-point `transform` with a single
     vectorised rasterisation of the sampled points (project all sample coords to
     pixel space once, then a numpy gather). This is the largest single win and
     affects both `roads_s` and `graph_s`.
  2. cache the projected sample coordinates between Stages 6 and 7, so the road
     graph can reuse the S6 sample coordinates instead of re-interpolating.
- This was not done yet because it touches both modules and the 60k-building
  building sampling is comfortably under budget on its own; the 12.58 s is
  dominated by the road geometry, not by the building sampling.

### Spike row (g0_spike.py)

The spike script now emits a single Stage 6+7 row with counts and timings from
the live Trishuli run:

```
STAGE 6+7  buildings_affected=1219  buildings_possibly_affected=0
           buildings_total=60024  road_km_affected=31.061
           bridges_possibly_impacted=28  settlements_cut_off=1
           settlements_still_connected=59  settlements_no_pre_event_access=2
           road_km_severed=6.1745  s6_s=6.479  s7_s=6.096  s6+s7=12.576
```

### Requirements

`shapely`, `networkx` and `pyarrow` are already in `requirements.txt` and
`requirements-dev.txt` -> **no new dependencies** were added for Stages 6 and 7.
`pyarrow` is already required to count ohsome v2 GeoParquet rows; `networkx` is
the solver for the road graph; `shapely` is already the geometry library for the
whole pipeline ( DEM window, building footprints, road interpolation).

## Still unverified

[...same as before...]
