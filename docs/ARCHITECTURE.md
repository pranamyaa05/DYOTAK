# DYOTAK — Architecture

Companion to `PLAN.md`. Where they conflict, `PLAN.md` rules (especially the No-Hardcoding Policy) win.

---

## 1. System overview

```
 Browser (React + Vite + MapLibre GL)           built to /frontend/dist
        │   same origin, no CORS
        ▼
 FastAPI (uvicorn, 1 worker)  ── serves static frontend + /api/*
        │
        ├── Job manager (in-memory registry + disk persistence)
        │        │
        │        ▼
        │   ProcessPoolExecutor (CPU-bound stages)
        │        │
        │        ▼
        │   Pipeline:  pairing → fetch → flood_model → terrain_filter
        │              → osm → damage → isolation → facts → report
        │
        ├── Artifact store   (disk: data/cache, data/jobs)
        ├── Copilot service  (tool-calling over facts.json, validator)
        └── Report service   (WeasyPrint, EN/NE templates)

 External (allowed inputs only):
   CDSE Catalog + Process API (Sentinel-1, Sentinel-2)
   ohsome API (pre-event OSM snapshot)
   Copernicus DEM 30 m (public COG)
   LLM provider (optional; copilot only)

 Offline, never imported by /app:
   /training (PyTorch)    /eval (Kuro Siwo, hold-out, EMSR927 check-only)
```

### Design principles
1. **facts.json is the single source of numbers.**
2. **Stages are pure and idempotent**: same inputs plus same config produce the same artifacts.
3. **Everything tunable is in `/config`.**
4. **Every result has provenance.**
5. **Fail loudly and honestly**: typed errors, never a silent bad map.
6. **No torch in production.**

## 2. Repository layout

```
/app
  main.py                 app factory, static mount, lifespan
  settings.py             pydantic-settings, reads env and /config
  api/                    routers: presets, preflight, jobs, layers, upstream,
                          copilot, report, tiles, eval, health
  jobs/                   manager, registry, persistence, cancellation
  pipeline/
    cdse.py               auth, catalog, process API clips
    pairing.py            orbit/direction pairing rules
    flood_baseline.py     log-ratio change detection
    flood_model.py        ONNX tiled inference
    terrain.py            slope, HAND, shadow/layover masks
    water_mask.py         permanent-water exclusion
    optical.py            S2 indices, cloud estimate, debris cues
    ohsome.py             pre-event OSM extraction
    damage.py             overlay and classification
    isolation.py          graph analysis
    upstream.py           flow path tracing
    landing.py            candidate landing zones
    facts.py              builds facts.json
    provenance.py         provenance model
  copilot/                tools, prompts, renderer, validator, fallback
  report/                 templates (en, ne), renderer, fonts
  common/                 geo utils, errors, cache, logging, retry
/config
  default.yaml            all tunables (each with origin comment)
  presets/*.yaml          named AOIs (data only)
  i18n/                   backend message catalogues en.yaml, ne.yaml
/training                 own requirements, loaders, model, train, export, parity
/eval                     scripts, reference/ (check-only), results/
/contracts                openapi.json, schemas/, examples/, mock server
/frontend                 UI source
/scripts                  g0_spike.py, check_no_hardcoding.py, warmup.py
/docs                     PLAN, ARCHITECTURE, LIMITATIONS, ATTRIBUTION, KNOWN_ISSUES
/tests
Dockerfile  docker-compose.yml  .env.example  requirements.lock
```

## 3. Configuration system

- `config/default.yaml` is loaded into a typed pydantic model. Unknown keys fail startup.
- Values override via env (`DYOTAK_*`) for deployment differences only.
- A `config_hash` is computed and included in cache keys and provenance, so changing a threshold invalidates cached results automatically.
- Each parameter has `value`, and a comment: `origin: literature | calibrated(<split>) | engineering`.

Parameter groups (names only; values live in the file):
`aoi` (max side km), `pairing` (day offsets, max pre scenes, allow fallback flags), `fetch` (resolution, timeouts, retries), `flood` (threshold, tile size, overlap, speckle filter size), `terrain` (slope cutoff, HAND cutoff), `optical` (cloud limit, index thresholds), `osm` (snapshot policy, feature filters), `damage` (overlap fractions, confidence tiers, bridge buffer), `isolation` (flood threshold on edges, facility tags, place tags), `upstream` (accumulation threshold, HAND corridor), `landing` (slope, min area, distance), `copilot` (model, max tokens, tool list), `jobs` (stage timeouts, concurrency), `cache` (root, TTL policy).

## 4. Data flow and stages

Each stage reads typed inputs, writes artifacts to `data/jobs/<job_id>/`, and records timing and warnings. Raw clips are cached in `data/cache/` keyed by `hash(bbox, time, product, version)`.

| # | Stage | Input | Output | Failure codes |
|---|---|---|---|---|
| 1 | **pairing** | bbox, event_date | `pair.json`: post scene, pre scenes, relative orbit, direction, offsets, flags | `NO_POST_EVENT_SCENE`, `NO_VALID_ORBIT_PAIR`, `CDSE_UNAVAILABLE` |
| 2 | **fetch** | pair, bbox | S1 pre/post VV/VH rasters, DEM clip, S2 if clear | `CDSE_UNAVAILABLE`, `AOI_TOO_LARGE` |
| 3 | **flood_model** | S1 stack + DEM channels | `flood_probability.tif` | `MODEL_LOW_CONFIDENCE` (warning) |
| 4 | **terrain_filter** | probability, slope, HAND, permanent water | filtered probability, polygons | none |
| 5 | **osm** | bbox, snapshot date | buildings, roads, bridges, facilities, places (GeoParquet) | `OHSOME_UNAVAILABLE` |
| 6 | **damage** | filtered probability, OSM layers | classified layers with `status`, `confidence` | none |
| 7 | **isolation** | roads, probability, facilities, places | settlement classes, distances, ranks | none |
| 8 | **facts** | all above | `facts.json` | none |
| 9 | **report** | facts, templates | PDFs (generated lazily on request) | none |

### 4.1 Pairing rules (stage 1)
- Query Catalog for S1 GRD IW scenes intersecting the bbox, reading **relative orbit** and **orbit direction** from scene metadata.
- Post scene = first acquisition at or after the event date covering the bbox.
- Pre scenes = acquisitions with the **same relative orbit and same direction** at offsets from config (primary: one repeat cycle before; fallbacks flagged `degraded`).
- Median over up to N pre scenes (config).
- Rule check is a pure function with unit tests. Any mismatch raises `NO_VALID_ORBIT_PAIR`.
- Repeat-cycle length is read from config and verified against catalog data at G0, not assumed.

### 4.2 Fetch (stage 2)
- Catalog finds scenes; the Process API returns a clipped raster for bbox and time window. Terrain correction and units (dB or linear) are set explicitly and recorded in provenance.
- Exact request parameters are taken from current CDSE documentation at G0 and stored in `docs/KNOWN_ISSUES.md` if anything is unverified.
- Sentinel-2: cloud estimate computed on the clip; if above the config limit, S2 layers are skipped with a warning and the UI banner "Radar only".
- Resolution and size are bounded by the AOI cap to keep Processing Unit use predictable.

### 4.3 Flood model (stage 3)
- **Input channels:** pre VV, pre VH, post VV, post VH (dB, clipped to configured range, per-image normalised) plus slope (and optionally HAND).
- **Network:** U-Net with a small encoder (ResNet18 or MobileNetV3), exported to ONNX.
- **Inference:** tiled with overlap and cosine blending; CPU `onnxruntime`; thread count from config.
- **Output:** per-pixel flood probability in [0,1], plus a tile-level uncertainty summary. `MODEL_LOW_CONFIDENCE` is raised as a warning when the distribution is uncertain (rule in config).
- **Baseline:** `flood_baseline.py` computes same-orbit log-ratio change. Both outputs are retained; the selected one feeds downstream, and both are exposed in Evidence for comparison. Selection is by option and recorded in provenance. If the model file is missing or fails its self-check, the baseline is used and provenance says `degraded: model_unavailable`.

### 4.4 Terrain filtering (stage 4)
- Slope, HAND (from DEM conditioning), and radar shadow/layover masks derived from DEM and incidence geometry.
- Permanent water from pre-event imagery excluded from "new flooding".
- Output polygons: `flood_polygons`, and `debris_polygons` when S2 cues are available. Debris layers state their weaker confidence.

### 4.5 OSM extraction (stage 5)
- ohsome queried with an explicit `time` parameter. **Assertion:** `snapshot_date < event_date`, enforced in code and tested.
- Snapshot date policy in config (latest available before event), and the actual date used is written to provenance.
- Features: buildings, highways, bridges, hospitals and clinics, towns, and `place=*` settlements.

### 4.6 Damage (stage 6)
- Work in a metric CRS selected from the AOI centroid (UTM zone computed, not hardcoded).
- For each feature: probability-weighted overlap with the flood layer yields `status` ∈ {affected, possibly_affected, unaffected} and `confidence` tier, using fractions from config.
- Roads: segment-level status. Bridges: only `possibly_impacted`, never "destroyed".
- Per-type summaries go to facts.

### 4.7 Isolation (stage 7)
- **Graph:** undirected NetworkX graph; nodes are junctions/endpoints, edges are road segments with length and flood probability.
- **Destinations:** hospitals/clinics and towns from OSM. If none exist in the AOI, the stage returns a clear "no destinations" state with a limitation note instead of guessing.
- **Pre-event baseline:** compute reachability and shortest distance with the full graph.
- **Post-event:** remove edges whose flood probability exceeds the config threshold; recompute.
- **Settlement attachment:** each settlement (place node or building cluster) snaps to its nearest road node within a config distance.
- **Classes:**
  1. `newly_cut_off`: connected pre-event, no path post-event
  2. `still_connected`: path exists; record `extra_distance_km`
  3. `no_pre_event_access`: no path pre-event (not attributed to the flood)
- **Priority rank:** from buildings affected and isolation severity; formula components and weights are in config and recorded in facts.
- Edge cases covered by tests: disconnected graph, no facilities, all edges removed, settlement far from any road.

### 4.8 Facts (stage 8)
- Builds `facts.json` (schema in section 7). Every fact has an `id`, `value`, `unit`, and `source_stage`. Counts are computed from layers by code, never typed.

### 4.9 Upstream tracing (bonus, separate endpoint)
- Fetch DEM for a window around the clicked point, extending downstream (extent from config).
- Condition (fill depressions, resolve flats), compute D8 flow direction and accumulation (`pysheds` or equivalent).
- Snap the click to the nearest cell above the accumulation threshold, trace downstream.
- Corridor from a HAND threshold around the path; list settlements inside it, ordered by distance downstream.
- Output labelled "terrain-based estimate, not a hydraulic simulation". If a job is supplied, intersect with that job's settlements and classes.

### 4.10 Landing zones (add-on)
- From DEM slope and open-area connectivity near cut-off settlements; thresholds in config; output labelled "candidate, needs ground verification".

## 5. Job system

- `POST /api/jobs` validates, computes the cache key, and:
  - if a complete artifact set exists: returns `202` with a job whose result is immediately available and provenance `cached`;
  - else enqueues and returns `202 {job_id}`.
- **Registry:** in-memory dict guarded by a lock, mirrored to `data/jobs/<id>/job.json` on each transition. On startup, jobs are reloaded; any job found `running` is marked `failed: INTERNAL (interrupted)`.
- **Execution:** one pipeline task per job; CPU-bound stages in a `ProcessPoolExecutor`; I/O stages async with timeouts.
- **Concurrency:** limited by config (queue length shown to the user). Over-limit requests receive a clear "busy" message.
- **Cancellation:** cooperative flag checked between stages and inside long loops.
- **Status shape:** `status` (queued/running/succeeded/failed/cancelled), `stage`, `progress`, `stages[]` (name, state, started, ended, warnings), `warnings[]`, `error{code, message_key, params}`.
- **Why one uvicorn worker:** the registry is process-local. Scaling is not a goal; correctness is.

## 6. Caching and provenance

**Cache layers**
1. Raw clips: `hash(bbox, time, product, processing_params, version)`.
2. OSM extracts: `hash(bbox, snapshot_date, feature_set)`.
3. Stage artifacts per job: `hash(bbox, dates, model_version, osm_snapshot, config_hash)`.

**Provenance object (on every result)**
```
mode: live | cached | degraded
computed_at, code_version, config_hash, model_version
scenes: {post: id/time, pre: [ids/times], relative_orbit, direction}
s2: {used: bool, cloud_pct, scene_id}
osm_snapshot_date
degradations: [ {code, reason_key} ]
```
Rules: `cached` always shows the original compute time. `degraded` always lists reasons. A `/api/admin/clear-cache` is not exposed publicly; a CLI command does it, and the "delete cache recomputes" test uses it.

## 7. Data contracts

### Endpoints
| Endpoint | Purpose |
|---|---|
| `GET /api/presets` | Preset AOIs from `config/presets` |
| `POST /api/preflight` | Scene availability, orbit, pair, cloud estimate, AOI check; no heavy work |
| `POST /api/jobs` | Start job |
| `GET /api/jobs/{id}` | Status |
| `POST /api/jobs/{id}/cancel` | Cancel |
| `GET /api/jobs/{id}/result` | Manifest |
| `GET /api/jobs/{id}/layers/{name}` | GeoJSON or PNG + bounds |
| `POST /api/upstream` | Flow path and settlements |
| `POST /api/copilot/ask` | Guarded answer |
| `GET /api/jobs/{id}/report?lang=en\|ne` | One-page PDF |
| `GET /api/tiles/terrain/{z}/{x}/{y}.png` | Terrain-RGB |
| `GET /api/eval/summary` | Model vs baseline metrics from `eval/results` |
| `GET /api/eval/emsr927` | Precomputed comparison (labelled) |
| `GET /api/health`, `GET /api/selftest` | Liveness, end-to-end check |

### Layers
- **Rasters (PNG + bounds):** `s1_pre`, `s1_post`, `s2_pre`, `s2_post` (if clear), `flood_probability`, `uncertainty`.
- **Vectors (GeoJSON):** `flood_polygons`, `debris_polygons`, `buildings`, `roads`, `bridges`, `settlements`, `facilities`, `landing_zones`, and for upstream `flow_path`, `corridor`.

### Error codes
`AOI_TOO_LARGE`, `NO_VALID_ORBIT_PAIR`, `NO_POST_EVENT_SCENE`, `CDSE_UNAVAILABLE`, `OHSOME_UNAVAILABLE`, `MODEL_LOW_CONFIDENCE` (warning), `S2_TOO_CLOUDY` (warning), `BUSY`, `INTERNAL`.
Errors carry `message_key` and `params`; the frontend and backend catalogues render EN/NE text. No free-text English strings are sent as the only message.

### facts.json (shape)
```
{
  "meta": { job_id, bbox, event_date, provenance, units },
  "facts": [
    { "id": "flooded_area_km2", "value": <number>, "unit": "km2", "source_stage": "terrain_filter" },
    { "id": "buildings_affected", ... }, { "id": "buildings_possibly_affected", ... },
    { "id": "road_km_affected", ... }, { "id": "bridges_possibly_impacted", ... },
    { "id": "settlements_newly_cut_off", ... }, { "id": "settlements_no_pre_event_access", ... },
    { "id": "settlements_still_connected", ... }
  ],
  "tables": { "cutoff_settlements": [ {name, class, buildings_affected, extra_distance_km, priority_rank} ] }
}
```
The set of fact IDs is defined in the schema, not in UI code. The UI renders whatever the schema lists.

## 8. ML architecture

### Training (offline, `/training`, own environment)
- **Data:** Kuro Siwo (primary), Sen1Floods11 (optional). Cite Bountos et al., 2024 and Bonafilia et al., 2020.
- **Splits:** by **event/region**, never by tile. Held-out events are listed in `training/splits.yaml` and never touched in training or tuning.
- **Himalayan generalisation:** identify scenes in the datasets from comparable terrain (including any Nepal event in Sen1Floods11). If an insufficient set exists, document how an additional small held-out set was built and labelled, and keep it out of training.
- **Loss and metrics:** Dice + BCE; IoU, F1, precision, recall per split.
- **Augmentation:** flips, rotations, speckle and gain jitter; slope-aware sampling to include steep-terrain negatives.
- **Export:** ONNX with fixed opset recorded in the model card; parity test against PyTorch on random and real tiles (max abs difference threshold in config).

### Preprocessing calibration
`training/calibrate.py` compares the dB distributions of the training data with our live Process API clips on matched scenes. Differences are documented in the model card and compensated (per-image normalisation, clipping range) rather than ignored.

### Evaluation (`/eval`)
- `eval/run_model_vs_baseline.py`: Kuro Siwo test events and held-out scenes; writes `eval/results/metrics.json`.
- `eval/emsr927_compare.py`: loads reference polygons from `eval/reference/` (check-only), compares with a pipeline run of the case study, writes `eval/results/emsr927.json` with IoU, F1, and map overlays for visual comparison.
- The report and Evidence tab read these files; nothing is typed by hand.
- **CI guard:** static import check that nothing under `/app` references `/eval` or the reference folder.

### Model card (`models/MODEL_CARD.md`)
Data, splits, metrics, calibration notes, known failure modes (shadow, wet snow, steep slopes), version, hash.

## 9. Copilot architecture

```
question → intent router → tool calls (read-only over facts.json)
         → LLM drafts prose with placeholders  e.g. "{settlements_newly_cut_off}"
         → deterministic renderer fills values (Devanagari digits if lang=ne)
         → validator checks:
              - every placeholder resolves to a known fact id
              - no numeral appears outside rendered placeholders
              - all referenced settlements exist in tables
         → pass: return answer + fact ids used
         → fail or LLM outage: return template answer, flag "template_fallback"
```
- The LLM never receives raw numbers to rephrase; it receives fact IDs, names and classes.
- Disclosed behaviour: "numbers are rendered by code from the maps, not generated by the language model."
- Questions outside the data ("how many people died?") get a refusal explaining the system only reports what it mapped.
- Nepali: reviewed templates for the report; for Q&A, the LLM writes Nepali prose with placeholders and the same validator applies.
- Provider, model name, timeouts from config and env. If no key is present, the copilot runs in template mode and says so.

## 10. Report architecture

- HTML templates (`report/templates/en.html`, `ne.html`) with placeholders filled from facts and tables.
- WeasyPrint renders A4 one-pager: title, event and AOI, provenance line, headline metrics, top priority cut-off settlements, key limitations, attribution footer.
- Fonts bundled in the image (Noto Sans Devanagari, a Latin sans). A test renders a Nepali sample with conjuncts and checks glyph coverage.
- Numbers formatted via locale utilities (Devanagari digits for `ne`).
- Attribution strings loaded from `docs/ATTRIBUTION.md` source so they are never retyped.

## 11. Frontend integration

- Single page workspace. State holds: AOI, event date, preflight result, job status, manifest, active layers, language.
- Flow: draw/select AOI → `preflight` → verdict → `jobs` → poll `jobs/{id}` → fetch manifest → add layers → panels read `facts.json`.
- All layer styling reads metadata from the manifest (class names, legends), so adding a fact or class does not require code change in the map layer logic beyond style tokens.
- Terrain: MapLibre terrain source pointed at `/api/tiles/terrain/...`.
- Swipe: compare control between pre and post raster layers.
- The UI shows `provenance.mode` as a persistent badge and lists `degradations`.
- Contract development uses `/contracts/mock` (a mock server serving examples) until live endpoints pass the same schema tests.

## 12. Deployment

- **Image:** `python:3.11-slim`, system libraries for rasterio/WeasyPrint, fonts, pinned `requirements.lock`, no `torch`. Frontend built in a multi-stage build and copied to `/app/static`.
- **Runtime:** `uvicorn app.main:app --workers 1`, port from env (7860 for Hugging Face Spaces).
- **Primary target:** Hugging Face Space (Docker). **Second target:** Render or Fly. **Judge path:** `docker compose up` with `.env.example`.
- **Startup:** load config, verify model file hash, run a small `selftest`, expose result at `/api/selftest`. Selftest failures do not crash the app; they show on `/api/health` and in the UI.
- **Persistence:** `data/` mounted as a volume where the platform allows; otherwise treated as ephemeral and rebuilt by the same code path.
- **Warm-up:** `scripts/warmup.py` hits health, selftest and one small run before judging to avoid cold starts.
- **Secrets:** CDSE client id/secret, LLM key through env only.
- **Observability:** structured JSON logs with job_id and stage; per-stage timings in job status; request ID middleware.

## 13. Security and compliance guards

| Guard | Mechanism |
|---|---|
| EMS/UNOSAT/post-event OSM never an input | Import guard test; `/eval` outside the `app` package; CI fails on reference to it |
| OSM snapshot before event | Assertion in `ohsome.py` plus test |
| Same-orbit comparison | Pure rule function, unit-tested; pipeline cannot proceed without it |
| No torch in prod | CI checks `requirements.lock` and `pip list` in image |
| Attributions present | Test checks footer, report, PDFs contain required strings |
| No victim imagery | Policy note; no imagery assets in repo besides satellite data |
| Input validation | bbox ordering and range, date range, AOI cap, request size limits |
| Rate limiting | Basic per-IP limiter on job creation and copilot |
| Secrets hygiene | `.env` ignored, secret scan in CI |

## 14. Failure behaviour (what the user sees)

| Situation | Behaviour |
|---|---|
| No valid same-orbit pair | Preflight blocks Run; message explains and suggests searching nearby dates |
| No post-event scene yet | Clear message with the latest available acquisition date |
| CDSE down or quota exhausted | `CDSE_UNAVAILABLE`; cached results still served if they exist for the same key, labelled `cached` |
| ohsome down | `OHSOME_UNAVAILABLE`; flood map still shown; damage and cut-off tabs explain they are unavailable |
| S2 cloudy | Banner "Radar only"; optical layers hidden |
| Model unavailable | Baseline used, `degraded: model_unavailable` |
| LLM unavailable | Template answers, flagged |
| Server restart mid-job | Job marked failed with explanation; user can rerun |
| AOI too large | Immediate message with the cap from config |

## 15. Testing map

| Area | Location | Notes |
|---|---|---|
| Pairing | `tests/pipeline/test_pairing.py` | Table-driven orbit and direction cases |
| Baseline | `tests/pipeline/test_baseline.py` | Synthetic arrays |
| Damage | `tests/pipeline/test_damage.py` | Hand-built geometries |
| Isolation | `tests/pipeline/test_isolation.py` | Small graphs, edge cases |
| Validator | `tests/copilot/test_validator.py` | Hallucinated numeral rejected |
| Reports | `tests/report/test_pdf.py` | Nepali glyph check |
| Contracts | `tests/api/test_contract.py` | OpenAPI and schema validation |
| Guards | `tests/guards/` | Import guard, OSM date, no-torch, attribution |
| Cache | `tests/jobs/test_cache.py` | Clear cache then recompute |
| ML | `training/tests/` | ONNX parity, tiling seams |
| E2E | `tests/e2e/test_selftest.py` | Small AOI through the real pipeline |

## 16. Open items to verify at G0 (do not assume)

- Exact CDSE Process API parameters for S1 terrain-corrected VV/VH and units
- Free Processing Unit quota and its effect on the AOI cap
- Actual repeat cycle and which Sentinel-1 satellites are acquiring over Nepal in the event window
- ohsome latest snapshot date and feature filters
- Which scenes in Kuro Siwo and Sen1Floods11 are Himalayan, for the hold-out
- WeasyPrint rendering of Devanagari conjuncts in the chosen base image

Record outcomes in `docs/KNOWN_ISSUES.md`.
