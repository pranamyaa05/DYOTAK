# DYOTAK — Project Plan

> Orbital intelligence for ground-level survival.
> Multimodal AI Hackathon 2026 · Track B: Mapping Flood Damage from Space
> Educational prototype. Not an operational tool.

---

## 1. Mission and success definition

Build a system that takes **any area and any flood date** and, from raw Sentinel data, answers three questions for rescue teams:

1. **Where did the flood hit?** (flooded and debris-covered areas)
2. **What was damaged?** (buildings, roads, bridges, from pre-event OpenStreetMap)
3. **Who is cut off?** (settlements with no road path to the nearest hospital or town)

Plus: the declared AI component, the bonus (upstream flood-path tracing), the Trishuli (EMSR927) case study, a dashboard, a one-page bilingual situation report, a report, a demo video and a GitHub repo.

**We succeed if** a judge draws an unfamiliar box, picks an unfamiliar date, and gets either a correct, honestly-labelled result or a clear, honest refusal explaining why no valid result exists. Both outcomes score. A silent bad map does not.

## 2. Scoring map (what we optimise for)

| Rubric item | Weight | Primary owner | What earns the marks |
|---|---|---|---|
| Flood and damage mapping quality | 30% | Backend/ML | Same-orbit change detection, terrain filtering, correct OSM overlay, live on unseen AOIs |
| AI component | 25% | Backend/ML | Trained segmentation model, event-level hold-out evaluation on unseen Himalayan scenes, baseline comparison |
| Cut-off settlement analysis | 20% | Backend/ML | Graph analysis with three honest classes, priority ranking |
| Report, Q&A, limitations | 15% | Both (frontend owner leads) | Candid limitations, correct EMSR927 comparison, we can explain every number |
| Dashboard and report usability | 10% | Frontend owner | Fast, clear, bilingual, accessible |

## 3. Declared decisions

- **Official AI component: flood segmentation model.**
- **Copilot is built as a guarded extra**, strictly after the segmentation model passes its gate. It never blocks the core.
- **Bonus (upstream tracing) is built**, after the MVP is deployed and stable.
- **Add-ons:** before/after swipe, 3D terrain, bilingual PDF, uncertainty layer, provenance panel, EMSR927 comparison tab, candidate landing zones.
- **Inference is ONNX Runtime only.** No PyTorch in the production image.
- **One origin.** FastAPI serves both API and built frontend.

## 4. The No-Hardcoding Policy (applies to every file)

This is a hard project rule, enforced by review and by tests.

1. **No result values in code.** No flood extent, counts, scores, settlement names, coordinates of damage or reference numbers appear in source. Every number the user sees comes from `facts.json`, produced by the pipeline for that run.
2. **No special-casing of the Trishuli area or any AOI or date.** Presets are plain data files (`config/presets/*.yaml`: name, bbox, event date). They are submitted through the exact same code path as any user input. A test asserts preset runs and arbitrary runs execute identical code.
3. **Tunable values live in `config/`**, never inline. Each threshold carries a comment stating its origin: literature, calibrated on a named validation split, or chosen by us and why. Examples: flood probability threshold, slope cut-off, HAND cut-off, AOI size cap, pairing day offsets, retry counts, timeouts.
4. **Caching is memoisation, not storage of answers.** Cache keys are `hash(bbox, dates, model_version, osm_snapshot, config_hash)`. Deleting the cache recomputes everything via the same path. Provenance states `live` or `cached (computed <timestamp>)`.
5. **Fallbacks are disclosed, never silent.** Every degraded path (older pair, S1-only, baseline instead of model, template instead of LLM) sets a visible `degraded` provenance with a reason code.
6. **Evaluation numbers are generated, not typed.** Metrics in the report and in the Evidence tab are read from `eval/results/*.json`, written by eval scripts. The report build script pulls numbers from those files.
7. **EMSR927 is check-only.** Reference data lives in `/eval/reference/` and is never imported by `/app` (CI guard). The comparison numbers are computed by `/eval/emsr927_compare.py`, then served read-only and labelled "precomputed validation".
8. **UI strings** live in translation files. UI never computes statistics.
9. **Secrets** only through environment variables.

A repo-wide check (`scripts/check_no_hardcoding.py`) scans `/app` for suspicious literals (coordinates, large numeric constants outside `config/`) and fails CI. It will produce false positives; the allow-list is explicit and reviewed.

## 5. Team and ownership

| Person | Owns |
|---|---|
| **A: Backend/ML/Deploy (you)** | Data access, pipeline, ML training and ONNX, jobs API, copilot, reports engine, Docker, CI, deployment, evaluation, `/contracts` |
| **B: Frontend and submission** | Dashboard UI/UX, i18n, map interactions, report document (max 6 pages), demo video, README screenshots, attribution page, submission checklist |

**Shared:** the contract (`/contracts`), Nepali translation review, limitations text, final rehearsal.

**Interface rule:** A publishes `openapi.json`, JSON schemas, example payloads and a **mock server** first. B builds entirely against mocks until live endpoints pass the same schemas. Contract changes require both people's agreement and a version bump.

## 6. Build gates

Work is ordered by dependency. A gate must pass before dependent work merges.

### G0 — Data access works (blocks everything)
**Exit criteria**
- CDSE authentication works with env credentials.
- Catalog search returns Sentinel-1 scenes with relative orbit, orbit direction, acquisition time.
- A clipped Sentinel-1 VV/VH fetch for a small bbox succeeds without downloading a full scene.
- A clipped Sentinel-2 L2A fetch with cloud information succeeds.
- ohsome returns buildings, highways, bridges, health facilities and places for a given pre-event timestamp.
- Copernicus DEM tile fetch works.
- `scripts/g0_spike.py` prints a pass/fail table.

**If clipping is blocked or quota is too small:** switch to range-reading Cloud-Optimised GeoTIFFs, or reduce the AOI cap. Record what could not be verified in `docs/KNOWN_ISSUES.md`. Exact CDSE parameters must be taken from current official docs, not memory.

### G1 — Honest baseline flood map
**Exit criteria**
- Same-orbit, same-direction pairing logic with a unit-tested rule set.
- Log-ratio change detection with speckle handling, permanent-water exclusion, slope and HAND masks.
- Output visible on the map via mock-free live endpoint.
- Clear refusal (`NO_VALID_ORBIT_PAIR`) when no valid pair exists.

### G2 — MVP: damage + isolation, deployed
**Exit criteria**
- Damage overlay and cut-off analysis produce all three settlement classes.
- Async job system, disk persistence, polling, cancellation.
- Deployed to a public URL; `/api/selftest` passes there.
- Frontend renders the live manifest.

**Nothing below starts until G2 is stable.**

### G3 — Segmentation model beats the baseline (or we say it doesn't)
**Exit criteria**
- Training split by **event**, not tile.
- Evaluation on (a) Kuro Siwo test events, (b) held-out Himalayan or comparable scenes, (c) baseline log-ratio on the same data.
- ONNX export with PyTorch-vs-ONNX parity test passing.
- Calibration check between training preprocessing and our live fetch preprocessing documented.
- Decision rule fixed in advance: **if the model does not beat the baseline on held-out data, we ship whichever is better and report that honestly.** No forcing.

### G4 — Reports and copilot
**Exit criteria**
- `facts.json` is the single source of numbers.
- One-page English and Nepali PDFs render correctly, including Devanagari conjuncts.
- Copilot validator rejects any numeral not in facts; outage path returns template answers with a visible flag.
- Nepali strings reviewed by a native speaker.

### G5 — Bonus and add-ons
Upstream tracing, swipe compare, 3D terrain, uncertainty layer, landing zones. Each is independent; ship what is stable. A feature that is flaky is hidden behind a flag, not left half-working.

### G6 — Evidence and submission
- EMSR927 comparison computed and displayed.
- Report (max 6 pages) with limitations.
- 3-minute demo video.
- Attribution strings in footer, README, report, PDFs.
- Rehearsal matrix passed on the **deployed** URL.

## 7. Workstreams and deliverables

### 7.1 Backend/ML (A)
- `app/pipeline/*` stages (see ARCHITECTURE.md)
- Training code and evaluation in `/training` and `/eval`
- Job system, caching, provenance, error taxonomy
- Copilot guard layer, report engine
- Docker, CI, deployment, monitoring of free-tier behaviour

### 7.2 Frontend and submission (B)
- Map workspace, panels and tabs per the frontend brief
- Bilingual UI with Devanagari digit formatting
- Swipe compare, 3D terrain toggle, popups, legends
- States for every component: empty, loading, partial, error, degraded
- Report document, demo video, README visuals, submission checklist

### 7.3 Shared
- Contract versions and examples
- Limitations text
- Rehearsal sessions

## 8. Risk register

| Risk | Impact | Mitigation | Early signal |
|---|---|---|---|
| CDSE clip API quota or access limits | Live runs fail | Cache raw clips by key; cap AOI; COG range-read alternative; pre-warm | G0 spike fails |
| No same-orbit pair for judge's date | No map | Preflight tells the user before running; clear refusal message; suggest nearest valid date | Preflight verdict |
| Domain shift: training preprocessing vs live fetch | Model produces junk | Calibration script day one; per-image normalisation; validate on live clips | Histogram mismatch |
| Radar shadow/layover false positives in mountains | Poor precision | Slope and HAND masks; DEM channels as model input; report as limitation | High flood area on steep slopes |
| Model does not beat baseline | AI score risk | Decision rule fixed in advance; report honestly; improve data and augmentation | G3 eval |
| Free-tier host sleeps or lacks RAM | Demo dies | Host with enough RAM; warm-up; second deploy target; `docker compose up` path | Cold-start time |
| FastAPI/ML dependency conflicts | Deploy break | No torch in prod; fully pinned lockfile; CI builds the image | CI image build |
| Job state lost on restart | Confusing UX | Persist job state and artifacts to disk | Restart test |
| LLM outage or hallucination | Bad copilot | Tool-calling only, placeholder prose, validator, template fallback | Validator tests |
| Nepali PDF glyph errors | Report unusable | WeasyPrint plus Noto Sans Devanagari; early test; browser print-CSS alternative | PDF visual check |
| OSM incomplete in remote areas | Misleading "cut off" | Class `no_pre_event_access`; limitation text | Many settlements with no road |
| Disqualification by data rule breach | Zero | Import guard, OSM date assertion, CI checks, review checklist | CI red |
| Judges see caching as hardcoding | Credibility loss | Provenance badges, delete-cache-recomputes test, show code path | Reviewer question |

## 9. Testing strategy

| Layer | Tests |
|---|---|
| Unit | Pairing rules, log-ratio math, graph classification, number validator, Devanagari digit conversion |
| Property/edge | Disconnected pre-event graph, no facilities, all roads flooded, empty AOI, antimeridian-free bbox checks |
| Contract | Every endpoint validated against `openapi.json`; example payloads validated against schemas |
| Integration | Real CDSE and ohsome calls on a small AOI (marked slow, run on schedule and before releases) |
| ML | Metrics regression threshold, ONNX parity (max abs diff), tiling seam test |
| Guards | `/app` never imports `/eval`; `osm_snapshot < event_date`; no `torch` in the prod lockfile |
| E2E | `/api/selftest` on startup and in CI Docker build |
| Rehearsal | Matrix below, on the deployed URL |

## 10. Rehearsal matrix (run on the deployed URL)

1. Trishuli case-study AOI and date (via preset)
2. Same AOI typed manually (must give identical results to the preset run)
3. Two unfamiliar Himalayan AOIs and dates
4. A non-Himalayan flood
5. An area and date with **no valid same-orbit pair**
6. A date with **no post-event scene**
7. An **oversized AOI**
8. A **cloudy Sentinel-2** case (S1-only path with banner)
9. A repeat of an earlier run (cached provenance shown)
10. Cache deleted, rerun (recomputes)
11. CDSE credentials missing or invalid (clean error)
12. Mobile browser, Nepali language, PDF download

Each failure gets a regression test.

## 11. Submission checklist

- [ ] Public GitHub repo with README (local run, Docker run, deployed URL)
- [ ] ONNX weights committed (Git LFS if needed)
- [ ] `.env.example` and `docker compose up` verified on a clean machine
- [ ] Dashboard live and warmed
- [ ] One-page situation report (EN and NE) generated by the system
- [ ] Report, max 6 pages, with limitations section
- [ ] 3-minute demo video, no victim imagery
- [ ] Case-study results compared with EMSR927 (check-only)
- [ ] Attribution strings in footer, README, report, PDFs (exact text in `ATTRIBUTION.md`)
- [ ] Training dataset citations (Bountos et al., 2024; Bonafilia et al., 2020 if used)
- [ ] EMSR credit line where reference data is displayed
- [ ] CI green, guards green

## 12. Limitations to document (start now, extend as we learn)

- Revisit time of days means no early warning for sudden glacier collapse
- Optical imagery is unusable under monsoon cloud; radar is the backbone
- Radar shadow and layover in steep terrain cause false positives and misses
- Debris detection from radar is weaker than water detection
- Flood extent is not flood depth
- OSM is crowd-sourced; pre-event data can be outdated or missing roads and villages
- Bridge integrity cannot be observed from orbit; only "possibly impacted"
- Terrain-based path tracing is not hydraulic simulation
- The same-orbit rule can make some areas and dates unmappable
- Exposure uses building counts, not population
- Model trained on datasets with different preprocessing and geography; transfer is measured, not assumed
- Educational prototype, not for operational decisions

## 13. Antigravity working agreement

- Rules file first (see section 14), then one phase at a time.
- Small tasks with tests; a task is not done if tests fail.
- The agent must not change pinned dependencies, restructure the repo, or alter `/contracts` without asking.
- The agent must state what it could not verify (especially external API parameters).
- Review every diff against the No-Hardcoding Policy before merging.

## 14. Rules file (paste into Antigravity workspace rules)

```
You are building DYOTAK. Read /docs/PLAN.md and /docs/ARCHITECTURE.md before
every task.

1. No hardcoded results, AOIs, dates, counts or thresholds in /app. Tunables
   live in /config with a comment on their origin.
2. Production image has no torch. Inference is onnxruntime only.
3. Python 3.11, FastAPI + pydantic v2, Shapely 2, rasterio wheels.
4. Never import or read Copernicus EMS, UNOSAT or post-event OSM in /app.
   EMSR927 data is read only from /eval.
5. Assert osm_snapshot_date < event_date.
6. Sentinel-1 comparison only on same relative orbit AND direction;
   otherwise raise NO_VALID_ORBIT_PAIR.
7. All user-visible numbers come from facts.json. Provenance is always set.
8. External calls: timeout, retry with backoff, typed error.
9. Write tests with every task and run them. Do not mark done on failure.
10. Follow /contracts/openapi.json exactly; stop and ask before changing it.
11. Do not change pinned dependencies or repo structure without asking.
12. If you cannot verify an external API parameter, say so explicitly.
```

## 15. Definition of done

A feature is done when: code merged, tests green, contract respected, no hardcoded values, provenance and error states handled, documented in README or ARCHITECTURE, limitations noted, and rehearsed on the deployed URL.
