"""Copernicus Data Space Ecosystem (CDSE) client — Stage 1/2 data access.

Three capabilities, all documented against live CDSE/Sentinel Hub docs:

1. OAuth2 client-credentials token
   POST https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token
   form body: grant_type=client_credentials, client_id, client_secret.

2. Catalog search for Sentinel-1 GRD IW and Sentinel-2 L2A
   CDSE STAC API (public, no auth for search):
   POST https://stac.dataspace.copernicus.eu/v1/search
   - collection "sentinel-1-grd": properties sat:relative_orbit (int),
     sat:orbit_state (ascending/descending), sar:instrument_mode (IW/EW/SM),
     datetime, id.
   - collection "sentinel-2-l2a": properties eo:cloud_cover, sat:relative_orbit,
     datetime, id.

3. Process API clips (Sentinel Hub on CDSE)
   POST https://sh.dataspace.copernicus.eu/api/v1/process
   - S1 GRD: input type "sentinel-1-grd"; processing.orthorectify=true,
     demInstance="COPERNICUS_30", backCoeff (default GAMMA0_ELLIPSOID);
     VV/VH requested together (polarization "DV", acquisitionMode "IW",
     resolution "HIGH"). Values converted to dB inside the evalscript so every
     scene shares one unit.
   - S2 L2A: input type "sentinel-2-l2a"; SCL band used for a cloud estimate
     (SCL classes 8/9/10 = clouds).

Raw clips are cached on disk keyed by hash(bbox, time, product, version) via
app.common.cache. The `version` component is a hash of the evalscript and
processing parameters, so changing them invalidates the cache automatically.

Everything tunable (resolution, timeouts, retries) is read from /config.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from app.common.cache import (
    compute_raw_clip_key,
    raw_cache_path,
    read_cached_bytes,
    write_cached_bytes,
)
from app.common.errors import CdseAuthError, CdseUnavailableError
from app.common.retry import retry_with_backoff
from app.settings import app_config, settings

logger = logging.getLogger("dyotak.cdse")

# ---------------------------------------------------------------------------
# Verified CDSE endpoints (see module docstring)
# ---------------------------------------------------------------------------
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
CDSE_STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
CDSE_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CDSE_STAC_S1_COLLECTION = "sentinel-1-grd"
CDSE_STAC_S2_COLLECTION = "sentinel-2-l2a"

# S1 backscatter coefficient. GAMMA0_ELLIPSOID is the documented Sentinel Hub
# default; kept explicit so the request and the cache version are unambiguous.
S1_BACKSCATTER_COEFFICIENT = "GAMMA0_ELLIPSOID"
S1_DEM_INSTANCE = "COPERNICUS_30"

# dB value produced by the evalscript's floor clamp: 10*log10(1e-6) = -60 dB.
# Pixels at the floor carry no usable backscatter and are treated as nodata.
S1_DB_FLOOR = -60.0


def s2_datatake_window_seconds() -> float:
    """Span of the Sentinel-2 clip time window, from config.

    The STAC `datetime` is the *datatake* start, but a given AOI is sensed some
    minutes later (a datatake lasts ~20-25 min), so a narrow window around the
    datatake start returns an empty, all-nodata mosaic. Widening the window to
    cover the whole datatake makes the scene actually contribute pixels. The
    measured value (and how it was measured) lives in config/default.yaml as
    `fetch.s2_datatake_window_seconds`.
    """
    return float(app_config.fetch.s2_datatake_window_seconds)


# SCL classes that count as cloud in the S2 cloud estimate.
S2_SCL_CLOUD_CLASSES = (8, 9, 10)  # medium prob, high prob, cirrus
S2_SCL_SHADOW_CLASS = 3
S2_SCL_NODATA = 0

# Evalscript returning VV and VH in decibels (units identical across scenes).
S1_DB_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: ["VV", "VH", "dataMask"],
    output: { bands: 3, sampleType: "FLOAT32" }
  };
}
function toDb(linear) {
  // 10 * log10(x); clamp to a small floor to avoid log(0).
  return 10 * Math.log(Math.max(linear, 1e-6)) / Math.LN10;
}
function evaluatePixel(samples) {
  // Band 3 is the dataMask (1 = valid, 0 = nodata) so nodata is explicit
  // instead of having to be inferred from the clamped dB floor.
  return [toDb(samples.VV), toDb(samples.VH), samples.dataMask];
}
"""

# Evalscript returning the Sentinel-2 Scene Classification Layer.
S2_SCL_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: ["SCL"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(samples) {
  return [samples.SCL];
}
"""

# Maximum output edge in pixels (keeps Processing Unit use predictable).
MAX_CLIP_PIXELS = 2500


@dataclass
class ClipResult:
    """A fetched (or cache-served) raw clip."""

    data: bytes
    path: Path
    from_cache: bool
    width: int = 0
    height: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
_TOKEN_LOCK = threading.Lock()
_TOKEN: Dict[str, Any] = {"value": None, "expires_at": 0.0}


def _credentials() -> tuple[Optional[str], Optional[str]]:
    return settings.cdse_client_id, settings.cdse_client_secret


def _token_timeout() -> float:
    return float(app_config.fetch.timeout_seconds)


def get_access_token(force_refresh: bool = False) -> str:
    """Return a cached CDSE access token, refreshing it when near expiry.

    The token endpoint is rate limited (CDSE docs), so tokens are cached in
    process and only re-fetched when close to expiry.
    """
    client_id, client_secret = _credentials()
    if not client_id or not client_secret:
        # Invalid credentials: fail fast, never retry.
        raise CdseAuthError(
            details="CDSE credentials missing (set DYOTAK_CDSE_CLIENT_ID / DYOTAK_CDSE_CLIENT_SECRET)"
        )

    with _TOKEN_LOCK:
        now = time.time()
        if not force_refresh and _TOKEN["value"] and _TOKEN["expires_at"] > now:
            return _TOKEN["value"]

        try:
            resp = requests.post(
                CDSE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_token_timeout(),
            )
        except requests.RequestException as exc:
            raise CdseUnavailableError(details=f"token request failed: {exc}") from exc

        if resp.status_code in (400, 401, 403):
            # 400 invalid_client / 401 / 403 -> auth failure, no retry.
            raise CdseAuthError(
                status_code=resp.status_code,
                details=f"token endpoint returned {resp.status_code}: {resp.text[:200]}",
            )
        if resp.status_code != 200:
            raise CdseUnavailableError(
                status_code=resp.status_code,
                details=f"token endpoint returned {resp.status_code}: {resp.text[:200]}",
            )

        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise CdseUnavailableError(details="token response missing access_token")

        expires_in = float(payload.get("expires_in", 600))
        _TOKEN["value"] = token
        _TOKEN["expires_at"] = now + max(expires_in - 60.0, 30.0)
        return token


def reset_token_cache() -> None:
    """Clear the in-process token cache (used by tests)."""
    with _TOKEN_LOCK:
        _TOKEN["value"] = None
        _TOKEN["expires_at"] = 0.0


# ---------------------------------------------------------------------------
# Date / geometry helpers (pure, unit-tested)
# ---------------------------------------------------------------------------
def _date_only(value: str) -> str:
    return value[:10]


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_plus_seconds(iso: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return _utc_iso(dt + timedelta(seconds=seconds))


def _bbox_to_output_size(bbox: List[float], resolution_m: float) -> tuple[int, int]:
    """Approximate a pixel grid for a WGS84 bbox at ~resolution_m metres.

    Uses a local metres-per-degree approximation (no pyproj dependency). The
    exact size is recorded in provenance; it only controls sampling density.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    if resolution_m <= 0:
        raise ValueError("resolution_m must be positive")
    lat_c = (min_lat + max_lat) / 2.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_c))
    width_m = abs(max_lon - min_lon) * m_per_deg_lon
    height_m = abs(max_lat - min_lat) * m_per_deg_lat
    width = max(1, int(round(width_m / resolution_m)))
    height = max(1, int(round(height_m / resolution_m)))
    if width > MAX_CLIP_PIXELS or height > MAX_CLIP_PIXELS:
        scale = MAX_CLIP_PIXELS / float(max(width, height))
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    return width, height


def _processing_version(payload: Dict[str, Any]) -> str:
    """Short hash of the request parts that change the pixels.

    The full dataFilter is included (time window included): changing the window
    changes which scene/pixels are returned, so the cached clip must be
    invalidated. The output pixel grid is not hashed here but is part of the
    cache key through the `product` component (see fetch_s1_clip /
    fetch_s2_scl_clip), so a resolution change also yields a fresh fetch.
    """
    material = {
        "evalscript": payload.get("evalscript"),
        "processing": payload["input"]["data"][0].get("processing"),
        "dataFilter": payload["input"]["data"][0].get("dataFilter", {}),
        "crs": payload["input"]["bounds"]["properties"].get("crs"),
    }
    encoded = json.dumps(material, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Catalog (CDSE STAC)
# ---------------------------------------------------------------------------
def _stac_search(body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    try:
        resp = requests.post(
            CDSE_STAC_SEARCH_URL,
            json=body,
            headers={"Content-Type": "application/geo+json", "Accept": "application/geo+json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CdseUnavailableError(details=f"STAC search failed: {exc}") from exc
    if resp.status_code in (401, 403):
        raise CdseAuthError(
            status_code=resp.status_code,
            details=f"STAC search returned {resp.status_code}: {resp.text[:200]}",
        )
    if resp.status_code != 200:
        raise CdseUnavailableError(
            status_code=resp.status_code,
            details=f"STAC search returned {resp.status_code}: {resp.text[:200]}",
        )
    return resp.json()


def _normalize_s1_item(feature: Dict[str, Any]) -> Dict[str, Any]:
    """Map a STAC sentinel-1-grd feature to the DYOTAK scene shape.

    Output keys match app.pipeline.pairing input: scene_id, acquisition_time,
    orbit_direction (upper-case), relative_orbit.
    """
    props = feature.get("properties", {}) or {}
    direction = props.get("sat:orbit_state")
    return {
        "scene_id": feature.get("id"),
        "acquisition_time": props.get("datetime"),
        "orbit_direction": direction.upper() if direction else None,
        "relative_orbit": props.get("sat:relative_orbit"),
        "platform": props.get("platform"),
        "instrument_mode": props.get("sar:instrument_mode"),
        "product_type": props.get("product:type"),
    }


def _normalize_s2_item(feature: Dict[str, Any]) -> Dict[str, Any]:
    props = feature.get("properties", {}) or {}
    return {
        "scene_id": feature.get("id"),
        "acquisition_time": props.get("datetime"),
        "cloud_pct": props.get("eo:cloud_cover"),
        "relative_orbit": props.get("sat:relative_orbit"),
        "platform": props.get("platform"),
    }


@retry_with_backoff(
    retries=app_config.fetch.retries,
    backoff_factor=app_config.fetch.backoff_factor,
    timeout=app_config.fetch.timeout_seconds,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc)),
)
def search_sentinel1_scenes(
    bbox: List[float],
    start_date: str,
    end_date: str,
    instrument_mode: str = "IW",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Search the CDSE STAC catalogue for Sentinel-1 GRD acquisitions.

    Returns scene dicts with scene_id, acquisition_time, orbit_direction and
    relative_orbit, sorted oldest-first.
    """
    body = {
        "collections": [CDSE_STAC_S1_COLLECTION],
        "bbox": [float(c) for c in bbox],
        "datetime": f"{_date_only(start_date)}T00:00:00Z/{_date_only(end_date)}T23:59:59Z",
        "limit": int(limit),
        "filter-lang": "cql2-json",
        "filter": {
            "op": "=",
            "args": [{"property": "sar:instrument_mode"}, instrument_mode],
        },
    }
    payload = _stac_search(body, _token_timeout())
    scenes = [_normalize_s1_item(f) for f in payload.get("features", [])]
    scenes = [s for s in scenes if s.get("scene_id") and s.get("acquisition_time")]
    scenes.sort(key=lambda s: s["acquisition_time"])
    return scenes


@retry_with_backoff(
    retries=app_config.fetch.retries,
    backoff_factor=app_config.fetch.backoff_factor,
    timeout=app_config.fetch.timeout_seconds,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc)),
)
def search_sentinel2_scenes(
    bbox: List[float],
    start_date: str,
    end_date: str,
    max_cloud: Optional[float] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Search the CDSE STAC catalogue for Sentinel-2 L2A acquisitions."""
    clauses = []
    if max_cloud is not None:
        clauses.append(
            {"op": "<=", "args": [{"property": "eo:cloud_cover"}, float(max_cloud)]}
        )
    body: Dict[str, Any] = {
        "collections": [CDSE_STAC_S2_COLLECTION],
        "bbox": [float(c) for c in bbox],
        "datetime": f"{_date_only(start_date)}T00:00:00Z/{_date_only(end_date)}T23:59:59Z",
        "limit": int(limit),
    }
    if clauses:
        body["filter-lang"] = "cql2-json"
        body["filter"] = clauses[0] if len(clauses) == 1 else {"op": "and", "args": clauses}

    payload = _stac_search(body, _token_timeout())
    scenes = [_normalize_s2_item(f) for f in payload.get("features", [])]
    scenes = [s for s in scenes if s.get("scene_id") and s.get("acquisition_time")]
    scenes.sort(key=lambda s: s["acquisition_time"])
    return scenes


# ---------------------------------------------------------------------------
# Process API (Sentinel Hub on CDSE)
# ---------------------------------------------------------------------------
def build_s1_process_request(
    bbox: List[float],
    acquisition_time: str,
    width: int,
    height: int,
    polarization: str = "DV",
    back_coeff: str = S1_BACKSCATTER_COEFFICIENT,
    dem_instance: str = S1_DEM_INSTANCE,
    orbit_direction: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the Process API request for an orthorectified VV/VH dB clip."""
    data_filter: Dict[str, Any] = {
        "acquisitionMode": "IW",
        "polarization": polarization,
        "resolution": "HIGH",
        "mosaickingOrder": "mostRecent",
        "timeRange": {
            "from": _utc_iso(datetime.fromisoformat(acquisition_time.replace("Z", "+00:00"))),
            "to": _iso_plus_seconds(acquisition_time, 60),
        },
    }
    if orbit_direction:
        data_filter["orbitDirection"] = orbit_direction.upper()

    return {
        "input": {
            "bounds": {
                "bbox": [float(c) for c in bbox],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": "sentinel-1-grd",
                    "dataFilter": data_filter,
                    "processing": {
                        "backCoeff": back_coeff,
                        "orthorectify": True,
                        "demInstance": dem_instance,
                    },
                }
            ],
        },
        "output": {
            "width": int(width),
            "height": int(height),
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": S1_DB_EVALSCRIPT,
    }


def build_s2_scl_process_request(
    bbox: List[float],
    acquisition_time: str,
    width: int,
    height: int,
) -> Dict[str, Any]:
    """Build the Process API request for a Sentinel-2 Scene Classification clip."""
    return {
        "input": {
            "bounds": {
                "bbox": [float(c) for c in bbox],
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "mosaickingOrder": "mostRecent",
                        "timeRange": {
                            "from": _utc_iso(
                                datetime.fromisoformat(acquisition_time.replace("Z", "+00:00"))
                            ),
                            "to": _iso_plus_seconds(
                                acquisition_time, s2_datatake_window_seconds()
                            ),
                        },
                    },
                }
            ],
        },
        "output": {
            "width": int(width),
            "height": int(height),
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": S2_SCL_EVALSCRIPT,
    }


def _post_process(payload: Dict[str, Any], timeout: float) -> bytes:
    token = get_access_token()
    try:
        resp = requests.post(
            CDSE_PROCESS_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "image/tiff",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CdseUnavailableError(details=f"process request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise CdseAuthError(
            status_code=resp.status_code,
            details=f"process API returned {resp.status_code}: {resp.content[:200]!r}",
        )
    if resp.status_code != 200:
        raise CdseUnavailableError(
            status_code=resp.status_code,
            details=f"process API returned {resp.status_code}: {resp.content[:200]!r}",
        )
    content = resp.content
    if not content:
        raise CdseUnavailableError(details="process API returned an empty body")
    return content


def _cache_root(cache_root: Optional[str]) -> str:
    return cache_root or app_config.cache.root_dir


@retry_with_backoff(
    retries=app_config.fetch.retries,
    backoff_factor=app_config.fetch.backoff_factor,
    timeout=app_config.fetch.timeout_seconds,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc)),
)
def fetch_s1_clip(
    bbox: List[float],
    scene: Dict[str, Any],
    resolution_m: Optional[float] = None,
    polarization: str = "DV",
    dem_instance: str = S1_DEM_INSTANCE,
    cache_root: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> ClipResult:
    """Fetch a Sentinel-1 VV/VH clip (orthorectified, dB), cached on disk.

    `scene` must carry acquisition_time and (optionally) orbit_direction, as
    returned by search_sentinel1_scenes.

    `progress` is an optional callback for long-running job systems; the same
    message is always written to the module logger.
    """
    res = resolution_m or app_config.fetch.resolution_m
    width, height = _bbox_to_output_size(bbox, res)
    payload = build_s1_process_request(
        bbox,
        scene["acquisition_time"],
        width,
        height,
        polarization=polarization,
        dem_instance=dem_instance,
        orbit_direction=scene.get("orbit_direction"),
    )
    product = (
        f"sentinel-1-grd:{polarization}:{S1_BACKSCATTER_COEFFICIENT}:{dem_instance}:dB"
        f":{width}x{height}"
    )
    version = _processing_version(payload)
    key = compute_raw_clip_key(bbox, scene["acquisition_time"], product, version)
    path = raw_cache_path(_cache_root(cache_root), "s1", key, ".tif")

    cached = read_cached_bytes(path)
    if cached is not None:
        return ClipResult(cached, path, True, width, height, {"cache_key": key})

    logger.info("CDSE S1 clip fetch scene=%s bbox=%s", scene.get("scene_id"), bbox)
    message = (
        f"Downloading S1 radar clip for bbox {bbox} at {res:g}m resolution "
        f"(scene {scene.get('scene_id')}); this may take a few seconds"
    )
    if progress is not None:
        progress(message)
    logger.info("%s", message)
    data = _post_process(payload, app_config.fetch.timeout_seconds)
    write_cached_bytes(path, data)
    return ClipResult(
        data, path, False, width, height,
        {"cache_key": key, "scene_id": scene.get("scene_id"), "resolution_m": res},
    )


@retry_with_backoff(
    retries=app_config.fetch.retries,
    backoff_factor=app_config.fetch.backoff_factor,
    timeout=app_config.fetch.timeout_seconds,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc)),
)
def fetch_s2_scl_clip(
    bbox: List[float],
    scene: Dict[str, Any],
    resolution_m: float = 20.0,
    cache_root: Optional[str] = None,
) -> ClipResult:
    """Fetch a Sentinel-2 L2A Scene Classification Layer clip, cached on disk."""
    width, height = _bbox_to_output_size(bbox, resolution_m)
    payload = build_s2_scl_process_request(bbox, scene["acquisition_time"], width, height)
    product = f"sentinel-2-l2a:SCL:{width}x{height}"
    version = _processing_version(payload)
    key = compute_raw_clip_key(bbox, scene["acquisition_time"], product, version)
    path = raw_cache_path(_cache_root(cache_root), "s2", key, ".tif")

    cached = read_cached_bytes(path)
    if cached is not None:
        return ClipResult(cached, path, True, width, height, {"cache_key": key})

    logger.info("CDSE S2 SCL clip fetch scene=%s bbox=%s", scene.get("scene_id"), bbox)
    data = _post_process(payload, app_config.fetch.timeout_seconds)
    write_cached_bytes(path, data)
    return ClipResult(
        data, path, False, width, height,
        {"cache_key": key, "scene_id": scene.get("scene_id"), "resolution_m": resolution_m},
    )


# ---------------------------------------------------------------------------
# Raster decoding + cloud estimate (pure helpers tested with synthetic arrays)
# ---------------------------------------------------------------------------
def read_raster_band(data: bytes):
    """Decode the first band of a GeoTIFF into a numpy array (lazy import)."""
    import io

    import numpy as np
    import tifffile

    array = tifffile.imread(io.BytesIO(data))
    array = np.asarray(array)
    if array.ndim == 3:
        array = array[..., 0]
    return array


def cloud_pct_from_scl(scl) -> Optional[float]:
    """Fraction (%) of valid SCL pixels that are cloud (classes 8/9/10).

    Returns ``None`` (never 0.0) when the clip has zero valid pixels: an empty
    or all-nodata clip means cloud cover is undefined, not 0%.
    """
    import numpy as np

    arr = np.asarray(scl)
    valid = arr != S2_SCL_NODATA
    valid_count = int(valid.sum())
    if valid_count == 0:
        logger.warning(
            "S2 SCL clip has zero valid pixels (all class %d); cloud cover is "
            "undefined, returning None",
            S2_SCL_NODATA,
        )
        return None
    cloud = np.isin(arr, S2_SCL_CLOUD_CLASSES) & valid
    return round(100.0 * float(cloud.sum()) / valid_count, 2)


def estimate_cloud_pct(clip: ClipResult) -> Optional[float]:
    """Cloud cover % (or None when undefined) for a fetched S2 SCL clip."""
    return cloud_pct_from_scl(read_raster_band(clip.data))


def scl_class_histogram(scl) -> Dict[str, Any]:
    """Per-class pixel counts (SCL classes 0-11) and the valid-pixel fraction.

    ``valid_pct`` is the percentage of pixels that are not nodata (class 0),
    which is the denominator the cloud estimate uses.
    """
    import numpy as np

    arr = np.asarray(scl)
    total = int(arr.size)
    counts = {c: int(np.count_nonzero(arr == c)) for c in range(12)}
    valid = int(np.count_nonzero(arr != S2_SCL_NODATA))
    return {
        "counts": counts,
        "total": total,
        "valid": valid,
        "valid_pct": round(100.0 * valid / total, 2) if total else 0.0,
    }
