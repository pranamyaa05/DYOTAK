"""Copernicus DEM 30m access from the public AWS Open Data bucket (Stage 2/4).

Source (verified): AWS Open Data bucket `copernicus-dem-30m`, public HTTPS.
Object layout (from the bucket readme):
    Copernicus_DSM_COG_10_<northing>_<easting>_DEM/
        Copernicus_DSM_COG_10_<northing>_<easting>_DEM.tif
where:
    [resolution] = 10 arc seconds for GLO-30,
    [northing]   = N|S + 2-digit degrees + "_00",
    [easting]    = E|W + 3-digit degrees + "_00".
Example (verified with a live HEAD request):
    https://copernicus-dem-30m.s3.amazonaws.com/
      Copernicus_DSM_COG_10_N27_00_E085_00_DEM/Copernicus_DSM_COG_10_N27_00_E085_00_DEM.tif

Tiles are Cloud-Optimised GeoTIFFs on a 1x1 degree grid, 3600x3600 float32,
~30 m (1 arc-second). Two access paths:

* ``read_dem_window`` (primary): opens the remote COG through GDAL
  ``/vsicurl/`` and reads ONLY the pixels covering the bbox (HTTP range reads).
  For a small AOI this transfers a few hundred KB instead of the full ~50-60 MB
  tile. The extracted window is cached on disk as a small GeoTIFF.
* ``fetch_dem_tile`` (fallback): downloads whole tiles; kept for environments
  where range reads are blocked.

Range reads need rasterio (GDAL). It is imported lazily so the rest of the
pipeline still works if rasterio is absent.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

from app.common.cache import (
    compute_raw_clip_key,
    raw_cache_path,
    read_cached_bytes,
    write_cached_bytes,
)
from app.common.errors import CdseUnavailableError
from app.common.retry import retry_with_backoff
from app.settings import app_config

logger = logging.getLogger("dyotak.dem")

# Make GDAL's /vsicurl/ behave for COG range reads: don't try to list the
# directory, only treat .tif as remotely openable, and forbid full-file
# pre-downloads triggered by sidecars.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")

DEM_30M_BUCKET_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
DEM_DATASET_VERSION = "2024_1"  # GLO-30 Public release; part of the cache key.

DEM_TILE_PIXELS = 3600          # COG height/width for GLO-30
DEM_PIXEL_DEG = 1.0 / 3600.0    # 1 arc-second at 1x longitude spacing (lat < 50)

# TIFF magic numbers (little- and big-endian).
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")


class DemUnavailableError(CdseUnavailableError):
    """DEM data could not be read (network, format, or missing rasterio)."""


@dataclass
class DemWindow:
    """A DEM window read for a bbox."""

    array: "np.ndarray"
    transform: Any
    crs: str
    path: Optional[Path]
    from_cache: bool
    tiles: List[str] = field(default_factory=list)

    @property
    def shape(self) -> Tuple[int, int]:
        return (int(self.array.shape[0]), int(self.array.shape[1]))


def _rasterio():
    """Import rasterio lazily and raise a typed error if unavailable."""
    try:
        import rasterio  # noqa: F401
        from rasterio.windows import Window  # noqa: F401

        return rasterio
    except Exception as exc:  # noqa: BLE001
        raise DemUnavailableError(
            details=f"rasterio is required for DEM range reads: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Tile geometry (pure, unit-tested)
# ---------------------------------------------------------------------------
def dem_tile_id(lat: float, lon: float) -> str:
    """Return the Copernicus DEM 30m tile id covering (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_"
        f"{ns}{abs(int(math.floor(lat))):02d}_00_"
        f"{ew}{abs(int(math.floor(lon))):03d}_00_DEM"
    )


def parse_tile_id(tile_id: str) -> Tuple[int, int]:
    """Return (lat_floor, lon_floor) for a tile id (south-west corner)."""
    parts = tile_id.split("_")
    if len(parts) < 9:
        raise ValueError(f"malformed DEM tile id: {tile_id}")
    ns, ew = parts[4], parts[6]
    lat = int(ns[1:])
    lon = int(ew[1:])
    if ns[0] == "S":
        lat = -lat
    if ew[0] == "W":
        lon = -lon
    return lat, lon


def dem_tile_url(tile_id: str) -> str:
    return f"{DEM_30M_BUCKET_URL}/{tile_id}/{tile_id}.tif"


def dem_tiles_for_bbox(bbox: List[float]) -> List[str]:
    """Return tile ids covering a WGS84 bbox [min_lon, min_lat, max_lon, max_lat]."""
    min_lon, min_lat, max_lon, max_lat = [float(c) for c in bbox]
    lat_lo, lat_hi = int(math.floor(min_lat)), int(math.floor(max_lat))
    lon_lo, lon_hi = int(math.floor(min_lon)), int(math.floor(max_lon))
    return [
        dem_tile_id(lat, lon)
        for lat in range(lat_lo, lat_hi + 1)
        for lon in range(lon_lo, lon_hi + 1)
    ]


def tile_window_for_subbbox(
    tile_id: str,
    sub_bbox: List[float],
    size: int = DEM_TILE_PIXELS,
    pixel: float = DEM_PIXEL_DEG,
) -> Tuple[int, int, int, int]:
    """Pixel window (row_off, col_off, height, width) for a sub-bbox inside a tile.

    The COG's top-left corner is (lon_floor, lat_floor + 1) because the shared
    south/east edge rows were removed upstream. Pure geometry, clamped to the tile.
    """
    lat0, lon0 = parse_tile_id(tile_id)
    origin_x = float(lon0)
    origin_y = float(lat0) + 1.0
    min_lon, min_lat, max_lon, max_lat = [float(c) for c in sub_bbox]

    # Epsilon (in pixels) so binary float error does not shift an exact edge
    # pixel: (85.15 - 85) / (1/3600) evaluates to 539.9999999999949, not 540.
    eps = 1e-6
    col_off = int(math.floor((min_lon - origin_x) / pixel + eps))
    row_off = int(math.floor((origin_y - max_lat) / pixel + eps))
    col_off = max(0, min(col_off, size - 1))
    row_off = max(0, min(row_off, size - 1))

    width = max(1, int(math.ceil((max_lon - min_lon) / pixel - eps)))
    height = max(1, int(math.ceil((max_lat - min_lat) / pixel - eps)))
    width = max(1, min(width, size - col_off))
    height = max(1, min(height, size - row_off))
    return row_off, col_off, height, width


def is_tiff(data: bytes) -> bool:
    return data[:4] in _TIFF_MAGIC


def _dem_cache_path(tile_id: str, cache_root: Optional[str]) -> Path:
    key = compute_raw_clip_key(
        bbox=[0.0, 0.0, 0.0, 0.0],
        time_window="static",
        product=f"copernicus-dem-30m:{tile_id}",
        version=DEM_DATASET_VERSION,
    )
    return raw_cache_path(cache_root or app_config.cache.root_dir, "dem", key, ".tif")


def _dem_window_cache_path(bbox: List[float], cache_root: Optional[str]) -> Path:
    key = compute_raw_clip_key(
        bbox=[float(c) for c in bbox],
        time_window="static",
        product="copernicus-dem-30m:window",
        version=f"{DEM_DATASET_VERSION}:{DEM_PIXEL_DEG:.10f}",
    )
    return raw_cache_path(cache_root or app_config.cache.root_dir, "dem_window", key, ".tif")


# ---------------------------------------------------------------------------
# Primary path: rasterio windowed HTTP range reads
# ---------------------------------------------------------------------------
def read_dem_window(bbox: List[float], cache_root: Optional[str] = None) -> DemWindow:
    """Read the DEM covering bbox using HTTP range reads on the remote COGs.

    Downloads only the pixels needed (plus COG overviews), not the whole tile.
    The extracted window is cached as a small GeoTIFF keyed by the bbox.
    """
    rasterio = _rasterio()
    from rasterio.windows import Window

    cache_path = _dem_window_cache_path(bbox, cache_root)
    if cache_path.is_file():
        with rasterio.open(cache_path) as ds:
            return DemWindow(
                array=ds.read(1),
                transform=ds.transform,
                crs=str(ds.crs),
                path=cache_path,
                from_cache=True,
                tiles=[],
            )

    min_lon, min_lat, max_lon, max_lat = [float(c) for c in bbox]
    width = max(1, int(round((max_lon - min_lon) / DEM_PIXEL_DEG)))
    height = max(1, int(round((max_lat - min_lat) / DEM_PIXEL_DEG)))
    out = np.full((height, width), np.nan, dtype="float32")
    transform = rasterio.transform.from_origin(min_lon, max_lat, DEM_PIXEL_DEG, DEM_PIXEL_DEG)

    tiles = dem_tiles_for_bbox(bbox)
    for tile_id in tiles:
        lat0, lon0 = parse_tile_id(tile_id)
        ixmin = max(min_lon, lon0)
        iymin = max(min_lat, lat0)
        ixmax = min(max_lon, lon0 + 1.0)
        iymax = min(max_lat, lat0 + 1.0)
        if ixmax <= ixmin or iymax <= iymin:
            continue  # bbox touches the tile edge only

        row_off, col_off, win_h, win_w = tile_window_for_subbbox(
            tile_id, [ixmin, iymin, ixmax, iymax]
        )
        url = "/vsicurl/" + dem_tile_url(tile_id)
        logger.info("DEM range read %s window=(%d,%d,%d,%d)", tile_id, col_off, row_off, win_w, win_h)
        try:
            with rasterio.open(url) as ds:
                block = ds.read(1, window=Window(col_off, row_off, win_w, win_h))
        except Exception as exc:  # noqa: BLE001 - wrap GDAL errors
            raise DemUnavailableError(details=f"DEM range read failed for {tile_id}: {exc}") from exc

        dest_col = int(round((ixmin - min_lon) / DEM_PIXEL_DEG))
        dest_row = int(round((max_lat - iymax) / DEM_PIXEL_DEG))
        dest_col = max(0, min(dest_col, width))
        dest_row = max(0, min(dest_row, height))
        copy_h = min(win_h, height - dest_row)
        copy_w = min(win_w, width - dest_col)
        out[dest_row:dest_row + copy_h, dest_col:dest_col + copy_w] = block[:copy_h, :copy_w]

    _write_window(cache_path, out, transform, "EPSG:4326", rasterio)
    return DemWindow(
        array=out, transform=transform, crs="EPSG:4326",
        path=cache_path, from_cache=False, tiles=tiles,
    )


def _write_window(path: Path, array, transform, crs: str, rasterio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=int(array.shape[0]),
        width=int(array.shape[1]),
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=np.nan,
        compress="deflate",
        tiled=True,
    ) as dst:
        dst.write(array.astype("float32"), 1)


# ---------------------------------------------------------------------------
# Fallback path: full tile download
# ---------------------------------------------------------------------------
@retry_with_backoff(
    retries=app_config.fetch.retries,
    backoff_factor=app_config.fetch.backoff_factor,
    timeout=app_config.fetch.timeout_seconds,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc)),
)
def fetch_dem_tile(tile_id: str, cache_root: Optional[str] = None) -> str:
    """Download a full DEM tile (fallback) and return its local path."""
    path = _dem_cache_path(tile_id, cache_root)
    if read_cached_bytes(path) is not None:
        return str(path)

    url = dem_tile_url(tile_id)
    logger.info("DEM full tile download %s", url)
    try:
        resp = requests.get(url, timeout=app_config.fetch.timeout_seconds)
    except requests.RequestException as exc:
        raise CdseUnavailableError(details=f"DEM request failed: {exc}") from exc

    if resp.status_code != 200:
        raise CdseUnavailableError(
            details=f"DEM tile {tile_id} returned HTTP {resp.status_code}"
        )
    if not is_tiff(resp.content):
        raise CdseUnavailableError(details=f"DEM tile {tile_id} is not a GeoTIFF")
    write_cached_bytes(path, resp.content)
    return str(path)


def fetch_dem_tiles_for_bbox(
    bbox: List[float],
    cache_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Download every DEM tile covering the bbox (fallback). No mosaicking."""
    results: List[Dict[str, Any]] = []
    for tile_id in dem_tiles_for_bbox(bbox):
        path = _dem_cache_path(tile_id, cache_root)
        was_cached = read_cached_bytes(path) is not None
        local = fetch_dem_tile(tile_id, cache_root=cache_root)
        data = Path(local).read_bytes()
        results.append(
            {
                "tile_id": tile_id,
                "url": dem_tile_url(tile_id),
                "path": local,
                "bytes": len(data),
                "from_cache": was_cached,
            }
        )
    return results
