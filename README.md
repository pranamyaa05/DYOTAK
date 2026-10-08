# DYOTAK
DYOTAK: Orbital intelligence for ground-level survival.

Satellite flood-damage mapping for rescue teams — Multimodal AI Hackathon 2026,
Track B. See `docs/PLAN.md` and `docs/ARCHITECTURE.md`.

## Data access & environment variables

Secrets are read from the environment only (never committed). Set:

| Variable | Required for | Notes |
|---|---|---|
| `DYOTAK_CDSE_CLIENT_ID` | CDSE OAuth + Sentinel-1/2 Process API clips | Create an OAuth client in the CDSE dashboard |
| `DYOTAK_CDSE_CLIENT_SECRET` | same | |
| `DYOTAK_OHSOME_API_KEY` | Pre-event OSM extraction (ohsome) | **Required for ohsome v2** (free key) |
| `DYOTAK_GEMINI_API_KEY` | Copilot Q&A (optional) | Copilot falls back to templates when absent |

### ohsome v2 requires an API key

Pre-event OSM extraction uses the ohsome **v2** Extraction API
(`https://api.heigit.org/ohsome-api/v2-rc/extraction/features.parquet`), which
**requires a free API key** (sign up at <https://api.heigit.org>). Without
`DYOTAK_OHSOME_API_KEY` the G0 spike reports the ohsome check as **SKIP**.

The legacy ohsome v1 `elements/geometry` endpoint currently answers **HTTP 403**
and v1 is scheduled for shutdown on 2026-11-30. A documented Overpass API
`[date:"…"]` attic fallback (for use if ohsome fails during judging) lives in
`app/pipeline/ohsome.py`; findings are recorded in `docs/KNOWN_ISSUES.md`.

## Copernicus DEM

Terrain data is read directly from the public AWS COG bucket
(`copernicus-dem-30m`) using rasterio windowed HTTP range reads, so only the
pixels covering the AOI are transferred (a few hundred KB), not whole ~60 MB
tiles. Full-tile download is kept as a fallback.

## Run the G0 data spike

```bash
python scripts/g0_spike.py            # Trishuli preset, PASS/FAIL/SKIP table
python scripts/g0_spike.py --skip-clips
```

## Tests

```bash
pytest
```
