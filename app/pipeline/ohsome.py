"""Pre-event OpenStreetMap extraction via the ohsome API (Stage 5).

Enforces .cursorrules Rule 4 & 5:
- Allowed inputs: OSM as of a snapshot date strictly BEFORE the event.
- NEVER import or read Copernicus EMS, UNOSAT or post-event OSM in /app.

Two backends are supported (selected by config `osm.backend`):

* ``v2`` (default, the supported API)
  POST {osm.v2_base_url}/extraction/features.parquet
  JSON body: {"filter": ..., "aoi": [min_lon,min_lat,max_lon,max_lat],
              "time": {"start": ..., "end": ...}, "clip": true}
  Header: Authorization: <DYOTAK_OHSOME_API_KEY>
  Response: GeoParquet (columns: osm_type, osm_id, tags, bbox, geom_type, geom(WKB), ...).

* ``v1`` (legacy, retained for reference; NOT usable today)
  POST {osm.base_url}/elements/geometry  (form fields bboxes/time/filter/properties)
  Returns GeoJSON. The v1 extraction endpoint currently answers HTTP 403 and v1
  is scheduled for shutdown on 2026-11-30 — see docs/KNOWN_ISSUES.md.

Raw extracts are cached on disk keyed by hash(bbox, time, product, version),
matching ARCHITECTURE.md Section 6 cache layer 2 (OSM extracts).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from typing import Any, Dict, List, Optional

import requests

from app.common.cache import (
    compute_raw_clip_key,
    raw_cache_path,
    read_cached_bytes,
    write_cached_bytes,
)
from app.common.errors import OhsomeAuthError, OhsomeUnavailableError
from app.common.retry import retry_with_backoff
from app.settings import app_config, settings

logger = logging.getLogger("dyotak.ohsome")

# Canonical ohsome filters for the feature tokens used in config/osm.feature_filters.
# Sources: ohsome filter reference (buildings/highways examples) + key=value selectors.
FEATURE_FILTERS: Dict[str, str] = {
    "building": "building=* and building!=no and geometry:polygon",
    "highway": (
        "type:way and ("
        "highway in (motorway, motorway_link, trunk, trunk_link, primary, "
        "primary_link, secondary, secondary_link, tertiary, tertiary_link, "
        "unclassified, residential, living_street, service, track, path) "
        ")"
    ),
    "bridge": "bridge=* and (geometry:line or geometry:polygon)",
    "amenity": (
        "(amenity=hospital or healthcare=hospital or amenity=clinic "
        "or healthcare=clinic) and (geometry:polygon or geometry:point or geometry:line)"
    ),
    "place": "place=* and (geometry:point or geometry:polygon)",
}


# =============================================================================
# FALLBACK PLAN — Overpass API with attic data, for use if ohsome fails during
# judging (key revoked, quota exhausted, v2-rc outage).
#
# Overpass exposes historical ("attic") data through the `[date:"..."]` global
# setting, which returns the OSM state AS OF that timestamp — exactly what
# .cursorrules Rule 5 requires (pre-event snapshot).
#
#   [out:json][timeout:60][date:"2024-09-25T00:00:00Z"];
#   (
#     way["building"](27.85,85.15,28.05,85.35);
#     way["highway"](27.85,85.15,28.05,85.35);
#   );
#   out geom;
#
#   POST https://overpass-api.de/api/interpreter   (form field: data=<query>)
#
# How it maps onto this module:
#   - build_overpass_attic_query() below produces that query from a bbox, a
#     snapshot date and the same feature tokens used by FEATURE_FILTERS.
#   - Parse the `elements` array (node/way/relation with `geometry`) into a
#     GeoJSON FeatureCollection tagged with properties["_dyotak_feature_type"],
#     then hand it to the same downstream stages as the v1 result.
#
# Caveats to state honestly before switching:
#   - Attic data is only available for changes present in the public history;
#     deleted features may be approximated. Coverage/retention is not guaranteed
#     by any SLA, so a date with no attic record yields an empty/incomplete set.
#   - Overpass is rate-limited and shared; do not loop over many feature types —
#     issue ONE combined query (build_overpass_attic_query does this).
#   - The `[date:]` value must be a full ISO-8601 UTC timestamp, not a bare date.
#   - Data is ODbL; attribution is required (see docs/ATTRIBUTION.md).
#   - Public instances: overpass-api.de, overpass.kumi.systems (fallback host).
# =============================================================================
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"

# Overpass QL element selectors per feature token (bbox is appended per feature).
OVERPASS_FILTERS: Dict[str, str] = {
    "building": 'way["building"]',
    "highway": 'way["highway"]',
    "bridge": 'way["bridge"]',
    "amenity": (
        'node["amenity"~"hospital|clinic"];'
        'way["amenity"~"hospital|clinic"];'
        'node["healthcare"~"hospital|clinic"];'
        'way["healthcare"~"hospital|clinic"]'
    ),
    "place": 'node["place"]',
}


def _overpass_timestamp(snapshot_date: str) -> str:
    """Normalise a date to the full ISO-8601 UTC timestamp Overpass [date:] needs."""
    return snapshot_date if "T" in snapshot_date else f"{snapshot_date}T00:00:00Z"


def build_overpass_attic_query(
    bbox: List[float],
    snapshot_date: str,
    feature_types: Optional[List[str]] = None,
) -> str:
    """Build a single Overpass QL query for a pre-event (attic) OSM snapshot.

    bbox is [min_lon, min_lat, max_lon, max_lat]; Overpass expects
    (south,west,north,east) = (min_lat, min_lon, max_lat, max_lon).
    """
    types = list(feature_types or app_config.osm.feature_filters)
    unknown = [t for t in types if t not in OVERPASS_FILTERS]
    if unknown:
        raise ValueError(f"Unknown Overpass feature type(s): {unknown}")
    min_lon, min_lat, max_lon, max_lat = [float(c) for c in bbox]
    box = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    clauses = "".join(f"  {OVERPASS_FILTERS[t]}({box});\n" for t in types)
    return (
        f'[out:json][timeout:60][date:"{_overpass_timestamp(snapshot_date)}"];\n'
        f"(\n{clauses});\n"
        f"out geom;"
    )


def validate_osm_snapshot_date(osm_snapshot_date: str, event_date: str) -> None:
    """Enforce .cursorrules Rule 5 with a hard assertion."""
    assert osm_snapshot_date < event_date, (
        f"Assertion failed: osm_snapshot_date ({osm_snapshot_date}) must be "
        f"strictly before event_date ({event_date})"
    )


def build_filter(feature_types: List[str]) -> str:
    """Combine requested feature tokens into a single valid ohsome filter."""
    unknown = [f for f in feature_types if f not in FEATURE_FILTERS]
    if unknown:
        raise ValueError(f"Unknown ohsome feature type(s): {unknown}")
    return " or ".join(f"({FEATURE_FILTERS[f]})" for f in feature_types)


def build_v2_request(bbox: List[float], snapshot_date: str, ohsome_filter: str) -> Dict[str, Any]:
    """Build the ohsome v2 extraction/features JSON body."""
    return {
        "filter": ohsome_filter,
        "aoi": [float(c) for c in bbox],
        # Point-in-time extraction. NOTE: start == end is inferred from the v1
        # single-timestamp semantics; not yet verified against a live key.
        "time": {"start": snapshot_date, "end": snapshot_date},
        "clip": True,
    }


def _extract_version(feature_types: List[str]) -> str:
    material = {"filters": [FEATURE_FILTERS[f] for f in feature_types]}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:8]


def _cache_root(cache_root: Optional[str]) -> str:
    return cache_root or app_config.cache.root_dir


# ---------------------------------------------------------------------------
# v1 (legacy GeoJSON) — kept for reference/tests; endpoint currently 403
# ---------------------------------------------------------------------------
@retry_with_backoff(
    retries=app_config.osm.retries,
    backoff_factor=1.5,
    timeout=app_config.osm.request_timeout_seconds,
    on_failure_raise=lambda exc: OhsomeUnavailableError(details=str(exc)),
)
def fetch_feature_collection(
    bbox: List[float],
    snapshot_date: str,
    ohsome_filter: str,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Fetch a GeoJSON FeatureCollection from the legacy ohsome v1 endpoint."""
    url = f"{app_config.osm.base_url.rstrip('/')}/elements/geometry"
    data = {
        "bboxes": ",".join(str(float(c)) for c in bbox),
        "time": snapshot_date,
        "filter": ohsome_filter,
        "properties": "tags",
    }
    try:
        resp = requests.post(
            url,
            data=data,
            headers={"Accept": "application/geo+json"},
            timeout=timeout or app_config.osm.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise OhsomeUnavailableError(details=f"ohsome request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise OhsomeAuthError(details=f"ohsome v1 returned {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise OhsomeUnavailableError(
            details=f"ohsome v1 returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


# ---------------------------------------------------------------------------
# v2 (supported GeoParquet extraction)
# ---------------------------------------------------------------------------
@retry_with_backoff(
    retries=app_config.osm.retries,
    backoff_factor=1.5,
    timeout=app_config.osm.request_timeout_seconds,
    on_failure_raise=lambda exc: OhsomeUnavailableError(details=str(exc)),
)
def fetch_features_parquet(
    bbox: List[float],
    snapshot_date: str,
    ohsome_filter: str,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> bytes:
    """Download a GeoParquet extraction from the ohsome v2 API."""
    key = api_key or settings.ohsome_api_key
    if not key:
        # Missing/invalid credentials: fail fast, never retry.
        raise OhsomeAuthError(
            details="ohsome v2 requires an API key (set DYOTAK_OHSOME_API_KEY)"
        )
    url = f"{app_config.osm.v2_base_url.rstrip('/')}/extraction/features.parquet"
    try:
        resp = requests.post(
            url,
            json=build_v2_request(bbox, snapshot_date, ohsome_filter),
            headers={"Authorization": key, "Accept": "application/octet-stream"},
            timeout=timeout or app_config.osm.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise OhsomeUnavailableError(details=f"ohsome v2 request failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise OhsomeAuthError(
            details=f"ohsome v2 returned {resp.status_code}: {resp.content[:200]!r}"
        )
    if resp.status_code != 200:
        raise OhsomeUnavailableError(
            details=f"ohsome v2 returned {resp.status_code}: {resp.content[:200]!r}"
        )
    if not resp.content:
        raise OhsomeUnavailableError(details="ohsome v2 returned an empty body")
    return resp.content


def count_parquet_features(data: bytes) -> Optional[int]:
    """Row count of a GeoParquet extract. Returns None if pyarrow is absent."""
    try:
        import pyarrow.parquet as pq

        return int(pq.read_table(io.BytesIO(data)).num_rows)
    except Exception:  # noqa: BLE001 - decoding is best-effort
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch_preevent_osm_elements(
    bbox: List[float],
    osm_snapshot_date: str,
    event_date: str,
    feature_types: Optional[List[str]] = None,
    backend: Optional[str] = None,
    cache_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch, cache and merge pre-event OSM features for the AOI.

    Guarantees osm_snapshot_date < event_date.

    v2 returns {"format": "geoparquet", "counts": {...}, "total": n, ...}.
    v1 returns a merged GeoJSON FeatureCollection (features tagged with
    properties["_dyotak_feature_type"]).
    """
    validate_osm_snapshot_date(osm_snapshot_date, event_date)
    types = list(feature_types or app_config.osm.feature_filters)
    resolved_backend = backend or app_config.osm.backend

    key = compute_raw_clip_key(
        bbox=[float(c) for c in bbox],
        time_window=osm_snapshot_date,
        product=f"ohsome:{resolved_backend}:" + ",".join(sorted(types)),
        version=_extract_version(types),
    )

    if resolved_backend == "v2":
        return _fetch_v2(bbox, osm_snapshot_date, types, key, cache_root)
    return _fetch_v1(bbox, osm_snapshot_date, types, key, cache_root)


def _fetch_v2(
    bbox: List[float],
    snapshot_date: str,
    types: List[str],
    key: str,
    cache_root: Optional[str],
) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    paths: Dict[str, str] = {}
    total = 0
    any_cached = True

    for feature_type in types:
        part_key = compute_raw_clip_key(
            bbox=[float(c) for c in bbox],
            time_window=snapshot_date,
            product=f"ohsome:v2:{feature_type}",
            version=_extract_version([feature_type]),
        )
        path = raw_cache_path(_cache_root(cache_root), "osm", part_key, ".parquet")
        data = read_cached_bytes(path)
        if data is None:
            any_cached = False
            data = fetch_features_parquet(bbox, snapshot_date, FEATURE_FILTERS[feature_type])
            write_cached_bytes(path, data)
        paths[feature_type] = str(path)
        n = count_parquet_features(data)
        if n is None:
            counts[feature_type] = -1  # decode unavailable (pyarrow missing)
        else:
            counts[feature_type] = n
            total += n

    return {
        "format": "geoparquet",
        "snapshot_date": snapshot_date,
        "feature_types": types,
        "counts": counts,
        "total": total,
        "parquet_paths": paths,
        "from_cache": any_cached,
    }


def _fetch_v1(
    bbox: List[float],
    snapshot_date: str,
    types: List[str],
    key: str,
    cache_root: Optional[str],
) -> Dict[str, Any]:
    path = raw_cache_path(_cache_root(cache_root), "osm", key, ".json")
    cached = read_cached_bytes(path)
    if cached is not None:
        result = json.loads(cached.decode("utf-8"))
        result["from_cache"] = True
        return result

    merged: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for feature_type in types:
        collection = fetch_feature_collection(bbox, snapshot_date, FEATURE_FILTERS[feature_type])
        features = collection.get("features", []) if isinstance(collection, dict) else []
        for feature in features:
            feature.setdefault("properties", {})["_dyotak_feature_type"] = feature_type
        merged.extend(features)
        counts[feature_type] = len(features)

    result = {
        "format": "geojson",
        "type": "FeatureCollection",
        "features": merged,
        "snapshot_date": snapshot_date,
        "feature_types": types,
        "counts": counts,
        "total": len(merged),
        "from_cache": False,
    }
    write_cached_bytes(path, json.dumps(result).encode("utf-8"))
    return result
