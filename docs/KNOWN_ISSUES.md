# DYOTAK — Known Issues & Unverified Parameters

Per ARCHITECTURE.md Section 16, outcomes of G0 data-access verification are
recorded here. "Verified" means checked against live services on 2026-10-08.

## Verified live

| Item | Endpoint / value | Result |
|---|---|---|
| CDSE Sentinel-1 GRD catalog | `POST https://stac.dataspace.copernicus.eu/v1/search`, collection `sentinel-1-grd` | 7 IW scenes for the Trishuli window; `sat:relative_orbit`, `sat:orbit_state`, `datetime`, `id` returned |
| CDSE Sentinel-2 L2A catalog | same endpoint, collection `sentinel-2-l2a` | 24 scenes; `eo:cloud_cover` returned |
| Same-orbit pairing | `app/pipeline/pairing.py` on live catalog output | orbit 19, DESCENDING, 2 pre scenes |
| Copernicus DEM 30m | `https://copernicus-dem-30m.s3.amazonaws.com/<tile>/<tile>.tif` | 2 tiles fetched, 3600x3600 float32 GeoTIFF, pixel scale 1 arc-second, tiepoints 85E 28N / 85E 29N |
| OAuth token URL | `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token` | From CDSE docs (not exercised — no credentials) |

## Could NOT verify (needs credentials / access)

1. **CDSE OAuth token + Process API clips.** No `DYOTAK_CDSE_CLIENT_ID` /
   `_SECRET` available, so the token exchange and both clips are unexercised.
   The request bodies follow the documented Sentinel Hub S1GRD/S2L2A schema,
   but the following remain unverified end-to-end:
   - S1 `polarization: "DV"` (VV+VH) actually returning both bands.
   - `processing.orthorectify=true` + `demInstance="COPERNICUS_30"` output.
   - `backCoeff="GAMMA0_ELLIPSOID"` and the dB conversion in the evalscript.
   - A 60-second `timeRange` window from the exact acquisition time pinning the
     intended scene (mosaicking behaviour).
2. **Process API path.** CDSE announced (2026-03-09) a new path format
   `/process/v1` alongside legacy `/api/v1/process`. This code uses the legacy
   path `https://sh.dataspace.copernicus.eu/api/v1/process` (both are stated to
   work). Switch to `/process/v1` before the legacy path is deprecated.
3. **S2 cloud estimate.** The SCL-class method (classes 8/9/10) is implemented
   and unit-tested on synthetic arrays, but never run on a real clip.
4. **S1 backscatter units.** Docs state values are linear power by default and
   dB requires an evalscript conversion; our dB evalscript has not been run
   against the live API to confirm the numeric range.
5. **ohsome extraction (BLOCKER).**
   - v1 `POST https://api.ohsome.org/v1/elements/geometry` returns **HTTP 403**
     (Apache "Forbidden") for anonymous requests, while `elements/count` and
     `/v1/metadata` return 200. The v1 extraction endpoint appears disabled.
   - v2 `POST https://api.heigit.org/ohsome-api/v2-rc/extraction/features.parquet`
     returns **HTTP 401** without a key and 403 with an invalid key. A free API
     key is required (`DYOTAK_OHSOME_API_KEY`).
   - v1 shut down 2026-11-30; v2 URL is still `-rc`.
   - v2 returns **GeoParquet**; counting features needs `pyarrow`, which is not
     installed, so counts show as `-1` when pyarrow is absent.
   - v2 point-in-time semantics (`time.start == time.end`) are **inferred** from
     v1 and not verified.
   - The five feature filters (building/highway/bridge/amenity/place) are written
     to the documented filter syntax but not run against the live API.

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

## Local dependency notes

- `requests`, `tifffile`, and `rasterio` (>=1.4) are in `requirements.txt`.
- DEM tiles use DEFLATE + floating-point predictor (PREDICTOR=3); rasterio/GDAL
  decodes them, so `imagecodecs` is no longer required.
- Range reads need outbound HTTPS to `copernicus-dem-30m.s3.amazonaws.com`.
