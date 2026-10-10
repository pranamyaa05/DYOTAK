"""Pre-event OpenStreetMap extraction via the ohsome API (Stage 5).

Enforces .cursorrules Rule 4 & 5:
- Allowed inputs: OSM as of a snapshot date strictly BEFORE the event.
- NEVER import or read Copernicus EMS, UNOSAT or post-event OSM in /app.

Two backends are supported (selected by config `osm.backend`):

* ``v2`` (default, the supported API)
  POST {osm.v2_base_url}/extraction/features.parquet
  JSON body: {"filter": ..., "aoi": [min_lon,min_lat,max_lon,max_lat],
              "time": {"start": "<iso-utc>", "end": "<iso-utc>"}, "clip": true}
  Header: Authorization: <DYOTAK_OHSOME_API_KEY>
  Response: GeoParquet (columns: osm_type, osm_id, tags, bbox, geom_type, geom(WKB), ...).
  Verified live: time.start/time.end MUST be full timezone-aware ISO-8601 UTC
  timestamps (a bare date is rejected with HTTP 422), and v2 requires
  end > start (a point window start == end is also rejected). The request is
  therefore sent first as a point window and, on 422, retried once as a
  one-day window (end = start + 1 day, still before the event date).

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.common.cache import (
    compute_raw_clip_key,
    raw_cache_path,
    read_cached_bytes,
    write_cached_bytes,
)
from app.common.errors import (
    OhsomeAuthError,
    OhsomeRequestError,
    OhsomeUnavailableError,
)
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


# ---------------------------------------------------------------------------
# Time handling (ohsome v2 requires timezone-aware ISO-8601 timestamps)
# ---------------------------------------------------------------------------
def _iso_utc(value: str) -> str:
    """Normalise a date (or date+time) to a timezone-aware ISO-8601 UTC string.

    ohsome v2 rejects a bare date such as ``2024-09-25`` with HTTP 422
    ("Input should be a valid datetime"); it requires a full timestamp with an
    explicit timezone, e.g. ``2024-09-25T00:00:00Z``.
    """
    return value if "T" in value else f"{value}T00:00:00Z"


def _plus_days(value: str, days: int) -> str:
    """Return ``value`` shifted by ``days`` as a full ISO-8601 UTC timestamp."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    shifted = dt.astimezone(timezone.utc) + timedelta(days=days)
    return shifted.strftime("%Y-%m-%dT%H:%M:%SZ")


#: A single instant (time.start == time.end). Documented v1 point-in-time form.
TIME_FORM_POINT = "point"
#: A one-day window (time.end == time.start + 1 day). v2 requires end > start,
#: so this is the form that v2 actually accepts.
TIME_FORM_DAY = "day_range"


@dataclass
class OhsomeExtract:
    """A v2 extraction plus the time form that actually succeeded."""

    data: bytes
    time_form: str
    time_start: str
    time_end: str

    @property
    def time_window(self) -> Dict[str, Any]:
        """Provenance record of the request window that produced these bytes."""
        return {"start": self.time_start, "end": self.time_end, "form": self.time_form}


def _overpass_timestamp(snapshot_date: str) -> str:
    """Normalise a date to the full ISO-8601 UTC timestamp Overpass [date:] needs."""
    return _iso_utc(snapshot_date)


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


def build_v2_request(
    bbox: List[float],
    snapshot_date: str,
    ohsome_filter: str,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ohsome v2 extraction/features JSON body.

    ``time.start`` and ``time.end`` are full timezone-aware ISO-8601 UTC
    timestamps (bare dates are rejected with HTTP 422). By default ``end``
    equals ``start`` (a point in time); pass ``end_date`` to widen the window.
    """
    start = _iso_utc(snapshot_date)
    end = _iso_utc(end_date) if end_date else start
    return {
        "filter": ohsome_filter,
        "aoi": [float(c) for c in bbox],
        "time": {"start": start, "end": end},
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
def _body_text(resp: requests.Response) -> str:
    """Full, untruncated response body for error reporting."""
    text = resp.text
    if not text:
        text = resp.content.decode("utf-8", errors="replace")
    return text


def _ohsome_http_error(label: str, status_code: int, body: str) -> Exception:
    """Classify a non-200 ohsome response into a typed, correctly-retryable error.

    401/403 -> auth (no retry); 5xx -> unavailable (retryable); any other 4xx
    -> request error (no retry). The full body is included verbatim.
    """
    detail = f"ohsome {label} returned {status_code}: {body}"
    if status_code in (401, 403):
        return OhsomeAuthError(details=detail)
    if status_code >= 500:
        return OhsomeUnavailableError(details=detail)
    return OhsomeRequestError(details=detail)


def _ohsome_unavailable_from(exc: Exception) -> Exception:
    """Map a retryable failure to OhsomeUnavailableError without nesting.

    If the failure is already an OhsomeUnavailableError, return it unchanged so
    the message is not wrapped inside itself.
    """
    if isinstance(exc, OhsomeUnavailableError):
        return exc
    return OhsomeUnavailableError(details=str(exc))


@retry_with_backoff(
    retries=app_config.osm.retries,
    backoff_factor=1.5,
    timeout=app_config.osm.request_timeout_seconds,
    on_failure_raise=_ohsome_unavailable_from,
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
    if resp.status_code != 200:
        raise _ohsome_http_error("v1", resp.status_code, _body_text(resp))
    return resp.json()


# ---------------------------------------------------------------------------
# v2 (supported GeoParquet extraction)
# ---------------------------------------------------------------------------
def _post_v2(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> requests.Response:
    """POST the extraction body. Timeouts/connection errors are retryable."""
    try:
        return requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise OhsomeUnavailableError(details=f"ohsome v2 request failed: {exc}") from exc


@retry_with_backoff(
    retries=app_config.osm.retries,
    backoff_factor=1.5,
    timeout=app_config.osm.request_timeout_seconds,
    on_failure_raise=_ohsome_unavailable_from,
)
def fetch_features_parquet(
    bbox: List[float],
    snapshot_date: str,
    ohsome_filter: str,
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
    event_date: Optional[str] = None,
) -> OhsomeExtract:
    """Download a GeoParquet extraction from the ohsome v2 API.

    Sends a point-in-time request (time.start == time.end) using full
    timezone-aware timestamps. If v2 rejects that window with HTTP 422 (it
    requires end > start), retries ONCE with end = start + 1 day, provided that
    day still precedes ``event_date`` (Rule 5). If both windows fail, the exact
    response bodies are reported.

    Only timeouts, connection errors and 5xx are retried; 401/403/422 and other
    4xx surface immediately. Returns an OhsomeExtract recording which time form
    succeeded so it can be written to provenance.
    """
    key = api_key or settings.ohsome_api_key
    if not key:
        # Missing/invalid credentials: fail fast, never retry.
        raise OhsomeAuthError(
            details="ohsome v2 requires an API key (set DYOTAK_OHSOME_API_KEY)"
        )
    url = f"{app_config.osm.v2_base_url.rstrip('/')}/extraction/features.parquet"
    headers = {"Authorization": key, "Accept": "application/octet-stream"}
    timeout_s = timeout or app_config.osm.request_timeout_seconds

    start = _iso_utc(snapshot_date)
    attempts = [(TIME_FORM_POINT, start, start)]
    # The one-day fallback is only added when it still precedes the event
    # (Rule 5). Never widen past the event date.
    day_end = _plus_days(snapshot_date, 1)
    if event_date is None or day_end[:10] < event_date:
        attempts.append((TIME_FORM_DAY, start, day_end))

    rejection_notes: List[str] = []
    for form, t_start, t_end in attempts:
        body = build_v2_request(bbox, t_start, ohsome_filter, end_date=t_end)
        resp = _post_v2(url, body, headers, timeout_s)
        if resp.status_code == 200:
            if not resp.content:
                raise OhsomeUnavailableError(details="ohsome v2 returned an empty body")
            return OhsomeExtract(resp.content, form, t_start, t_end)
        if resp.status_code == 422:
            # Rejected time window: remember the exact body and try the next form.
            rejection_notes.append(
                f"time[{form}] returned {resp.status_code}: {_body_text(resp)}"
            )
            continue
        # 401/403, 5xx and other 4xx: classify and raise (retry only if 5xx).
        raise _ohsome_http_error("v2", resp.status_code, _body_text(resp))

    raise OhsomeRequestError(
        details="ohsome v2 rejected every supported time window. " + " | ".join(rejection_notes)
    )


def count_parquet_features(data: bytes) -> Optional[int]:
    """Row count of a GeoParquet extract. Returns None if pyarrow is absent."""
    try:
        import pyarrow.parquet as pq

        return int(pq.read_table(io.BytesIO(data)).num_rows)
    except Exception:  # noqa: BLE001 - decoding is best-effort
        return None


def max_edit_timestamp(data: bytes) -> Optional[str]:
    """Newest OSM edit timestamp in a GeoParquet extract, as ISO-8601 UTC.

    This is the real data timestamp the API reports for the returned snapshot:
    if it is still before the event date, the extract provably contains no
    post-event edits (Rule 5). Returns None when the column is absent or
    pyarrow cannot decode the file.
    """
    try:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        table = pq.read_table(io.BytesIO(data), columns=["edit_timestamp"])
        column = table.column("edit_timestamp")
        if not pa.types.is_timestamp(column.type):
            return None
        scale = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}[column.type.unit]
        newest = pc.max(pc.cast(column, pa.int64())).as_py()
        if newest is None:
            return None
        moment = datetime.fromtimestamp(newest / scale, timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001 - best-effort provenance
        return None


def fetch_ohsome_metadata(
    api_key: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """GET {osm.v2_base_url}/metadata — the ohsome instance's data coverage.

    Returns e.g. ``{"apiVersion": "2.0.0",
    "temporalExtent": {"start": ..., "end": ...}}``; ``temporalExtent.end`` is
    the timestamp of the latest OSM snapshot the instance holds (much later than
    any pre-event date we request).
    """
    url = f"{app_config.osm.v2_base_url.rstrip('/')}/metadata"
    key = api_key or settings.ohsome_api_key
    headers = {"Authorization": key} if key else {}
    try:
        resp = requests.get(
            url, headers=headers, timeout=timeout or app_config.osm.request_timeout_seconds
        )
    except requests.RequestException as exc:
        raise OhsomeUnavailableError(
            details=f"ohsome v2 metadata request failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise _ohsome_http_error("v2 metadata", resp.status_code, _body_text(resp))
    return resp.json()


def latest_osm_snapshot(metadata: Dict[str, Any]) -> Optional[str]:
    """``temporalExtent.end`` (latest OSM data timestamp) from /metadata."""
    extent = (metadata or {}).get("temporalExtent") or {}
    end = extent.get("end")
    return str(end) if end else None


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
        return _fetch_v2(bbox, osm_snapshot_date, event_date, types, key, cache_root)
    return _fetch_v1(bbox, osm_snapshot_date, types, key, cache_root)


def _time_sidecar_path(path: Path) -> Path:
    return path.with_name(path.stem + ".meta.json")


def _read_time_window(path: Path, snapshot_date: str) -> Dict[str, Any]:
    """Provenance time window for a cached extract (sidecar, else best-effort)."""
    raw = read_cached_bytes(_time_sidecar_path(path))
    if raw:
        try:
            window = json.loads(raw.decode("utf-8"))
            if isinstance(window, dict) and window.get("start"):
                return window
        except (ValueError, UnicodeDecodeError):
            pass
    return {"start": _iso_utc(snapshot_date), "end": None, "form": "cached"}


def _fetch_v2(
    bbox: List[float],
    snapshot_date: str,
    event_date: str,
    types: List[str],
    key: str,
    cache_root: Optional[str],
) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    paths: Dict[str, str] = {}
    total = 0
    any_cached = True
    time_window: Optional[Dict[str, Any]] = None

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
            extract = fetch_features_parquet(
                bbox,
                snapshot_date,
                FEATURE_FILTERS[feature_type],
                event_date=event_date,
            )
            data = extract.data
            write_cached_bytes(path, data)
            window = extract.time_window
            # Persist which time form worked, so a cache hit still reports it.
            write_cached_bytes(
                _time_sidecar_path(path), json.dumps(window).encode("utf-8")
            )
        else:
            window = _read_time_window(path, snapshot_date)
        if time_window is None:
            time_window = window
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
        "time_window": time_window,
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
