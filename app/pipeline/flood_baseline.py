"""Log-ratio change detection baseline (ARCHITECTURE.md Section 4.3 / PLAN.md G1).

This is the **honest baseline flood map**: no trained model, no new data source,
no calibration against EMSR927 or any published flood map. It only uses the
same-orbit pre/post Sentinel-1 clips fetched by ``app.pipeline.cdse``, the
windowed Copernicus DEM read by ``app.pipeline.dem``, and thresholds that live
in ``config/default.yaml`` with origin comments.

Pipeline (each step is a pure numpy function with unit tests):

1. **Speckle reduction** — median filter on the dB bands
   (``flood.speckle_filter_size``), applied to post and to every pre scene.
2. **Pre-event reference** — per-pixel median of up to
   ``pairing.max_pre_scenes`` same-orbit pre scenes (a single scene when only
   one exists). NaN (nodata) pixels are ignored by the median.
3. **Change detection** — ``log-ratio = post_dB - pre_dB`` per polarization.
   The polarizations are combined into one ratio by the configured weights
   (``flood.vv_weight`` / ``flood.vh_weight``).
4. **Threshold** — Otsu's method on the histogram of the combined log-ratio
   (``flood.otsu_histogram_bins``). A flood scene is bimodal (unchanged land
   near 0 dB, water several dB lower), so Otsu splits the two modes without a
   hand-picked constant. The split is clamped to the configured fallback range
   ``flood.otsu_fallback_min_db`` .. ``flood.otsu_fallback_max_db``; when Otsu
   lands outside it, the nearest bound is used and the method is recorded as
   ``otsu_fallback``. Nothing is calibrated against a reference flood map.
5. **Probability** — logistic ramp around the threshold
   (``flood.probability_scale_db``), giving a probability-like value in [0,1]
   (it is a monotone score, **not** a calibrated probability). The binary
   candidate mask is ``probability >= flood.probability_threshold``.
6. **Exclusions** (each counted separately so every drop is auditable):
   permanent water from the pre-event image (``water_mask``), slope and radar
   shadow/layover (``terrain``), and HAND above ``terrain.hand_cutoff_m``.
7. **Cleaning** — connected components (8-connectivity) smaller than
   ``flood.min_component_size_px`` are dropped.
8. **Output** — cleaned probability raster, cleaned binary mask, polygons as a
   GeoJSON FeatureCollection in EPSG:4326, and a step-by-step pixel count dict.
   Flood area is measured in the UTM zone of the AOI centroid
   (``app.common.geo.get_utm_epsg_for_bbox``), never a hardcoded zone.

Refusal: :func:`fetch_and_build_flood_map` runs the same-orbit pairing rule
first and raises ``NO_VALID_ORBIT_PAIR`` (``NoValidOrbitPairError``) when no
valid pre/post pair exists. In that case no raster, mask or polygon is
produced.

Documented limitations: absolute GAMMA0 calibration is not verified, the clips
are orthorectified but not radiometrically terrain-corrected, HAND here is the
proxy described in ``terrain.py`` (no sink filling), and the probability score
is not calibrated (see docs/KNOWN_ISSUES.md).
"""

from __future__ import annotations

import json
import logging
import warnings
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.common import geo
from app.pipeline import dem
from app.pipeline.cdse import S1_DB_FLOOR, fetch_s1_clip
from app.pipeline.pairing import validate_and_pair_s1_scenes
from app.pipeline.terrain import (
    compute_hand,
    compute_shadow_layover_mask,
    compute_slope_degrees,
    hand_exclusion_mask,
    metres_per_pixel,
    radar_look_azimuth_deg,
    slope_exclusion_mask,
)
from app.pipeline.water_mask import permanent_water_mask
from app.settings import app_config

logger = logging.getLogger("dyotak.flood_baseline")

#: 8-connected neighbourhood as (row delta, col delta), used by the component
#: labelling and the HAND breadth-first search.
NEIGHBOURS_8: Tuple[Tuple[int, int], ...] = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)
)

#: Step names in the order they are applied; every one gets a pixel count.
STEP_NAMES: Tuple[str, ...] = (
    "grid_pixels",
    "valid_overlap_pixels",
    "candidate_pixels",
    "excluded_permanent_water",
    "excluded_slope",
    "excluded_radar_shadow",
    "excluded_hand",
    "excluded_small_components",
    "final_mask_pixels",
)


@dataclass
class FloodMapResult:
    """Baseline flood map plus the audit trail of every step."""

    probability: np.ndarray                 # float32 in [0,1], 0 where excluded/nodata
    mask: np.ndarray                        # bool, cleaned binary mask
    polygons: Dict[str, Any]                # GeoJSON FeatureCollection (EPSG:4326)
    area_km2: float
    counts: Dict[str, int]
    threshold_db: Optional[float]
    threshold_method: str
    utm_epsg: int
    crs: str
    transform: Any = None
    probability_raw: Optional[np.ndarray] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> Tuple[int, int]:
        return (int(self.mask.shape[0]), int(self.mask.shape[1]))

    @property
    def is_empty(self) -> bool:
        return self.counts.get("final_mask_pixels", 0) == 0


# ---------------------------------------------------------------------------
# Sentinel-1 clip decoding (shared with the G0 spike)
# ---------------------------------------------------------------------------
def split_s1_bands(arr) -> Tuple[List[np.ndarray], Optional[np.ndarray]]:
    """Split a decoded Sentinel-1 clip into (value bands, dataMask).

    The evalscript returns [VV, VH, dataMask]; older cached clips may still
    return only [VV, VH]. tifffile may hand back (H, W, B) or (B, H, W).
    """
    arr = np.asarray(arr)
    if arr.ndim == 2:
        return [arr], None
    if arr.ndim == 3 and arr.shape[-1] in (2, 3):
        bands = [arr[..., 0], arr[..., 1]]
        return bands, (arr[..., 2] if arr.shape[-1] == 3 else None)
    if arr.ndim == 3 and arr.shape[0] in (2, 3):
        bands = [arr[0], arr[1]]
        return bands, (arr[2] if arr.shape[0] == 3 else None)
    return [arr], None


def band_masks(band, data_mask=None) -> Dict[str, np.ndarray]:
    """Boolean masks for one band: finite / inside dataMask / at floor / valid.

    A pixel is usable when it is finite, inside the evalscript's dataMask, and
    not sitting at the dB floor the evalscript clamps no-backscatter pixels to.
    """
    band = np.asarray(band, dtype="float64")
    finite = np.isfinite(band)
    if data_mask is None:
        masked_in = np.ones(band.shape, dtype=bool)
    else:
        mask = np.asarray(data_mask, dtype="float64")
        masked_in = (mask > 0) if mask.shape == band.shape else np.ones(
            band.shape, dtype=bool
        )
    at_floor = band <= S1_DB_FLOOR
    return {
        "finite": finite,
        "masked_in": masked_in,
        "at_floor": at_floor,
        "valid": finite & masked_in & ~at_floor,
    }


def decode_s1_clip(data: bytes) -> Tuple[List[np.ndarray], Optional[np.ndarray]]:
    """Decode a Sentinel-1 GeoTIFF clip into (value bands, dataMask)."""
    import io

    import tifffile

    return split_s1_bands(np.asarray(tifffile.imread(io.BytesIO(data))))


def clip_band_pair(data: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """Decode a clip into (VV, VH) dB arrays with NaN for every nodata pixel."""
    bands, data_mask = decode_s1_clip(data)
    if len(bands) < 2:
        raise ValueError("Sentinel-1 clip does not contain both polarizations")
    out = []
    for band in bands[:2]:
        valid = band_masks(band, data_mask)["valid"]
        values = np.asarray(band, dtype="float32")
        out.append(np.where(valid, values, np.nan).astype("float32"))
    return out[0], out[1]


def clip_grid(data: bytes) -> Dict[str, Any]:
    """Shape and WGS84 bounds of a clip, read from the GeoTIFF georeferencing."""
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as ds:
        bounds = ds.bounds
        return {
            "shape": (int(ds.height), int(ds.width)),
            "bounds": tuple(
                round(float(v), 9)
                for v in (bounds.left, bounds.bottom, bounds.right, bounds.top)
            ),
            "crs": ds.crs.to_string() if ds.crs else None,
        }


def grids_match(post_grid: Dict[str, Any], pre_grid: Dict[str, Any], tolerance: float = 1e-6) -> Optional[str]:
    """None when both clips share one grid (shape and bounds), else the reason."""
    if post_grid["shape"] != pre_grid["shape"]:
        return f"shape {post_grid['shape']} != {pre_grid['shape']}"
    a, b = post_grid["bounds"], pre_grid["bounds"]
    if any(abs(x - y) > tolerance for x, y in zip(a, b)):
        return f"bounds {a} != {b}"
    return None


# ---------------------------------------------------------------------------
# Step 1-2: speckle reduction and the pre-event reference
# ---------------------------------------------------------------------------
def speckle_filter_db(band_db: np.ndarray, window: Optional[int] = None, config: Any = None) -> np.ndarray:
    """Median speckle filter on a dB band (window from ``flood.speckle_filter_size``).

    A median over a small window removes the salt-and-pepper look of single-look
    speckle without moving edges. NaN (nodata) pixels are ignored and stay NaN;
    an even window is rounded up to the next odd size.
    """
    cfg = config or app_config
    size = int(window if window is not None else cfg.flood.speckle_filter_size)
    arr = np.asarray(band_db, dtype="float32")
    if size <= 1:
        return arr.copy()
    if size % 2 == 0:
        size += 1
    half = size // 2
    # Pad with NaN, not with edge values: fabricating a neighbour would turn a
    # nodata pixel into data (and inflate the valid-overlap mask downstream).
    padded = np.pad(arr, half, mode="constant", constant_values=np.nan)
    height, width = arr.shape
    stack = np.empty((size * size, height, width), dtype="float32")
    position = 0
    for row in range(size):
        for col in range(size):
            stack[position] = padded[row:row + height, col:col + width]
            position += 1
    with warnings.catch_warnings():
        # An all-NaN neighbourhood is legitimate nodata, not a numerical error.
        warnings.simplefilter("ignore", RuntimeWarning)
        filtered = np.nanmedian(stack, axis=0)
    # A nodata pixel stays nodata even when its neighbours are valid.
    filtered = np.where(np.isfinite(arr), filtered, np.nan)
    return filtered.astype("float32")


def median_pre_composite(pre_bands: Sequence[np.ndarray]) -> np.ndarray:
    """Per-pixel median of up to N pre-event dB bands (NaN if all are nodata)."""
    if len(pre_bands) == 0:
        raise ValueError("at least one pre-event band is required")
    stack = np.stack([np.asarray(band, dtype="float32") for band in pre_bands])
    if stack.shape[0] == 1:
        return stack[0].copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        composite = np.nanmedian(stack, axis=0)
    return composite.astype("float32")


# ---------------------------------------------------------------------------
# Step 3-5: change detection, threshold, probability
# ---------------------------------------------------------------------------
def log_ratio_db(post_db: np.ndarray, pre_db: np.ndarray) -> np.ndarray:
    """post_dB - pre_dB (negative means the surface got darker, i.e. wetter)."""
    post = np.asarray(post_db, dtype="float32")
    pre = np.asarray(pre_db, dtype="float32")
    if post.shape != pre.shape:
        raise ValueError("post and pre bands must share one grid")
    return (post - pre).astype("float32")


def combine_polarizations(
    vv_ratio_db: np.ndarray,
    vh_ratio_db: np.ndarray,
    vv_weight: Optional[float] = None,
    vh_weight: Optional[float] = None,
    config: Any = None,
) -> np.ndarray:
    """Combine the two log-ratios into one score by the configured weights.

    The combined VV+VH rule is a weighted mean of the two log-ratios. Both
    weights come from config (``flood.vv_weight`` / ``flood.vh_weight``); a
    weight may be 0, in which case that polarization is ignored. NaN in either
    input makes the combination NaN so nodata never votes.
    """
    cfg = config or app_config
    w_vv = float(vv_weight if vv_weight is not None else cfg.flood.vv_weight)
    w_vh = float(vh_weight if vh_weight is not None else cfg.flood.vh_weight)
    total = w_vv + w_vh
    if total <= 0:
        raise ValueError("combined polarization weights must sum to a positive value")
    vv = np.asarray(vv_ratio_db, dtype="float32")
    vh = np.asarray(vh_ratio_db, dtype="float32")
    if vv.shape != vh.shape:
        raise ValueError("polarization ratios must share one grid")
    combined = (w_vv * vv + w_vh * vh) / total
    return np.where(np.isfinite(vv) & np.isfinite(vh), combined, np.nan).astype("float32")


def otsu_threshold(
    values: np.ndarray,
    nbins: Optional[int] = None,
    fallback_min_db: Optional[float] = None,
    fallback_max_db: Optional[float] = None,
    config: Any = None,
) -> Tuple[Optional[float], str]:
    """Otsu split of the log-ratio histogram, clamped to the configured range.

    Returns ``(threshold_db, method)`` where ``method`` is:

    * ``"otsu"`` — the between-class variance maximum of the histogram, inside
      the configured range;
    * ``"otsu_fallback"`` — Otsu landed outside the range, so the nearest range
      bound is used (a badly conditioned histogram, e.g. an almost entirely
      flooded or almost entirely unchanged scene, cannot produce an absurd
      threshold);
    * ``"fallback_no_data"`` — fewer than two finite values, so nothing can be
      split; the middle of the configured range is used.
    """
    cfg = config or app_config
    bins = int(nbins if nbins is not None else cfg.flood.otsu_histogram_bins)
    low = float(
        fallback_min_db if fallback_min_db is not None else cfg.flood.otsu_fallback_min_db
    )
    high = float(
        fallback_max_db if fallback_max_db is not None else cfg.flood.otsu_fallback_max_db
    )
    if low > high:
        low, high = high, low
    midpoint = 0.5 * (low + high)

    arr = np.asarray(values, dtype="float64")
    finite = arr[np.isfinite(arr)]
    if finite.size < 2:
        return midpoint, "fallback_no_data"
    data_min, data_max = float(finite.min()), float(finite.max())
    if data_max - data_min <= 0:
        return midpoint, "fallback_no_data"

    counts, edges = np.histogram(finite, bins=max(2, bins), range=(data_min, data_max))
    centres = 0.5 * (edges[:-1] + edges[1:])
    total = float(counts.sum())
    weight_low = np.cumsum(counts) / total
    weight_high = 1.0 - weight_low
    cumulative_sum = np.cumsum(counts * centres)
    sum_total = float(cumulative_sum[-1])
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_low = np.where(
            np.cumsum(counts) > 0, cumulative_sum / np.maximum(np.cumsum(counts), 1), 0.0
        )
        mean_high = np.where(
            (total - np.cumsum(counts)) > 0,
            (sum_total - cumulative_sum) / np.maximum(total - np.cumsum(counts), 1),
            0.0,
        )
        between = np.where(
            (weight_low > 0) & (weight_high > 0),
            weight_low * weight_high * (mean_low - mean_high) ** 2,
            0.0,
        )
    split = int(np.argmax(between))
    threshold = float(centres[split])
    if threshold < low:
        return low, "otsu_fallback"
    if threshold > high:
        return high, "otsu_fallback"
    return threshold, "otsu"


def probability_from_log_ratio(
    ratio_db: np.ndarray,
    threshold_db: float,
    scale_db: Optional[float] = None,
    config: Any = None,
) -> np.ndarray:
    """Probability-like score in [0,1], logistic around the Otsu threshold.

    ``scale_db`` sets the ramp width: at ``threshold_db`` the score is 0.5 and a
    value ``scale_db`` lower is ~0.73. This is a monotone ranking score, not a
    calibrated probability (documented limitation).
    """
    cfg = config or app_config
    scale = float(scale_db if scale_db is not None else cfg.flood.probability_scale_db)
    if scale <= 0:
        raise ValueError("probability scale must be positive")
    ratio = np.asarray(ratio_db, dtype="float32")
    with np.errstate(over="ignore"):
        ramp = np.clip((ratio - threshold_db) / scale, -60.0, 60.0)
        score = 1.0 / (1.0 + np.exp(ramp))
    return score.astype("float32")


# ---------------------------------------------------------------------------
# Step 7: mask cleaning
# ---------------------------------------------------------------------------
def label_components(mask: np.ndarray, connectivity: int = 8) -> Tuple[np.ndarray, int]:
    """Label connected components of a boolean mask (8- or 4-connectivity)."""
    arr = np.asarray(mask, dtype=bool)
    labels = np.zeros(arr.shape, dtype="int32")
    offsets = NEIGHBOURS_8 if connectivity == 8 else ((-1, 0), (1, 0), (0, -1), (0, 1))
    height, width = arr.shape
    component = 0
    for row, col in zip(*np.nonzero(arr)):
        row, col = int(row), int(col)
        if labels[row, col]:
            continue
        component += 1
        labels[row, col] = component
        queue: deque = deque([(row, col)])
        while queue:
            r, c = queue.popleft()
            for dr, dc in offsets:
                nr, nc = r + dr, c + dc
                if 0 <= nr < height and 0 <= nc < width and arr[nr, nc] and not labels[nr, nc]:
                    labels[nr, nc] = component
                    queue.append((nr, nc))
    return labels, component


def clean_binary_mask(mask: np.ndarray, min_component_size: Optional[int] = None, config: Any = None) -> np.ndarray:
    """Drop connected components smaller than ``flood.min_component_size_px``."""
    cfg = config or app_config
    minimum = int(
        min_component_size
        if min_component_size is not None
        else cfg.flood.min_component_size_px
    )
    arr = np.asarray(mask, dtype=bool)
    if minimum <= 1:
        return arr.copy()
    labels, count = label_components(arr)
    if count == 0:
        return arr.copy()
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    keep = sizes >= minimum
    keep[0] = False
    return keep[labels]


# ---------------------------------------------------------------------------
# Step 8: outputs (polygons, area, writers)
# ---------------------------------------------------------------------------
def grid_transform(bbox: Sequence[float], shape: Tuple[int, int]) -> Any:
    """Affine transform of a north-up EPSG:4326 grid covering bbox."""
    from rasterio.transform import from_bounds

    min_lon, min_lat, max_lon, max_lat = [float(c) for c in bbox]
    height, width = int(shape[0]), int(shape[1])
    return from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)


def mask_to_geojson(
    mask: np.ndarray,
    transform: Any,
    crs: str = "EPSG:4326",
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Vectorize a boolean mask into a GeoJSON FeatureCollection.

    One feature per connected component; polygons are emitted in ``crs``
    (EPSG:4326 for the analysis grid, which is already lon/lat, so no
    reprojection is needed). Rasterization holes are preserved.
    """
    from rasterio.features import shapes

    arr = np.asarray(mask, dtype=bool)
    features: List[Dict[str, Any]] = []
    if not arr.any():
        return {"type": "FeatureCollection", "crs": _geojson_crs(crs), "features": []}

    labels, count = label_components(arr)
    for component in range(1, count + 1):
        component_mask = labels == component
        pixel_count = int(component_mask.sum())
        for geometry, _value in shapes(component_mask.astype("uint8"), mask=component_mask, transform=transform):
            feature_properties = {
                "component": component,
                "pixel_count": pixel_count,
            }
            if properties:
                feature_properties.update(properties)
            features.append(
                {
                    "type": "Feature",
                    "properties": feature_properties,
                    "geometry": geometry,
                }
            )
    return {"type": "FeatureCollection", "crs": _geojson_crs(crs), "features": features}


def _geojson_crs(crs: str) -> Dict[str, Any]:
    numbers = "".join(ch for ch in crs if ch.isdigit())
    if numbers:
        return {
            "type": "name",
            "properties": {"name": f"urn:ogc:def:crs:EPSG::{int(numbers)}"},
        }
    return {"type": "name", "properties": {"name": crs}}


def mask_area_km2(
    mask: np.ndarray,
    bbox: Sequence[float],
    utm_epsg: Optional[int] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Flood area in km2, measured on the UTM grid of the AOI centroid.

    The UTM zone is derived from the bbox centroid (never hardcoded) and the
    bbox is projected into it; the pixel area is the projected extent divided by
    the grid shape. Over a ~20 km AOI the residual scale variation of a single
    UTM zone is far below 1 % (documented approximation).
    """
    from rasterio.warp import transform as warp_transform

    arr = np.asarray(mask, dtype=bool)
    epsg = int(utm_epsg if utm_epsg is not None else geo.get_utm_epsg_for_bbox(list(bbox)))
    min_lon, min_lat, max_lon, max_lat = [float(c) for c in bbox]
    xs, ys = warp_transform(
        "EPSG:4326", f"EPSG:{epsg}", [min_lon, max_lon], [min_lat, max_lat]
    )
    width_m = abs(float(xs[1]) - float(xs[0]))
    height_m = abs(float(ys[1]) - float(ys[0]))
    height, width = arr.shape
    pixel_area_m2 = (width_m / width) * (height_m / height)
    pixels = int(arr.sum())
    area_km2 = pixels * pixel_area_m2 / 1e6
    meta = {
        "utm_epsg": epsg,
        "pixel_area_m2": round(pixel_area_m2, 3),
        "pixels": pixels,
        "area_km2": round(area_km2, 6),
    }
    return round(area_km2, 6), meta


def resample_nearest(
    source: np.ndarray,
    source_transform: Any,
    target_transform: Any,
    target_shape: Tuple[int, int],
    fill: float = float("nan"),
) -> np.ndarray:
    """Nearest-neighbour resample of a north-up raster onto another grid.

    Both grids are EPSG:4326 (lon/lat) north-up, so target pixel centres map to
    source indices with a simple affine per axis; no reprojection is involved.
    Targets outside the source extent are filled with ``fill``.
    """
    arr = np.asarray(source)
    target_height, target_width = int(target_shape[0]), int(target_shape[1])
    rows = np.arange(target_height)
    cols = np.arange(target_width)
    xs = float(target_transform.c) + (cols + 0.5) * float(target_transform.a)
    ys = float(target_transform.f) + (rows + 0.5) * float(target_transform.e)
    source_cols = np.floor((xs - float(source_transform.c)) / float(source_transform.a)).astype(int)
    source_rows = np.floor((ys - float(source_transform.f)) / float(source_transform.e)).astype(int)
    inside_cols = (source_cols >= 0) & (source_cols < arr.shape[1])
    inside_rows = (source_rows >= 0) & (source_rows < arr.shape[0])
    inside = np.outer(inside_rows, inside_cols)
    clipped_rows = np.clip(source_rows, 0, arr.shape[0] - 1)
    clipped_cols = np.clip(source_cols, 0, arr.shape[1] - 1)
    sampled = arr[np.ix_(clipped_rows, clipped_cols)]
    if np.issubdtype(arr.dtype, np.floating):
        return np.where(inside, sampled, np.float32(fill)).astype(arr.dtype)
    return np.where(inside, sampled, fill)


def write_flood_artifacts(result: FloodMapResult, out_dir: Path, stem: str = "flood") -> Dict[str, str]:
    """Write the probability raster, mask, polygons and step counts to disk."""
    import rasterio

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    transform = result.transform or grid_transform(result.provenance["bbox"], result.shape)
    profile = {
        "driver": "GTiff",
        "height": result.shape[0],
        "width": result.shape[1],
        "count": 1,
        "crs": result.crs,
        "transform": transform,
        "compress": "deflate",
    }
    probability_path = out_dir / f"{stem}_probability.tif"
    with rasterio.open(
        probability_path, "w", dtype="float32", nodata=0.0, **profile
    ) as dst:
        dst.write(np.asarray(result.probability, dtype="float32"), 1)
    mask_path = out_dir / f"{stem}_mask.tif"
    with rasterio.open(mask_path, "w", dtype="uint8", nodata=None, **profile) as dst:
        dst.write(result.mask.astype("uint8"), 1)
    polygons_path = out_dir / f"{stem}_polygons.geojson"
    polygons_path.write_text(json.dumps(result.polygons), encoding="utf-8")
    steps_path = out_dir / f"{stem}_steps.json"
    steps_path.write_text(
        json.dumps(
            {
                "counts": result.counts,
                "threshold_db": result.threshold_db,
                "threshold_method": result.threshold_method,
                "area_km2": result.area_km2,
                "utm_epsg": result.utm_epsg,
                "provenance": result.provenance,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "probability": str(probability_path),
        "mask": str(mask_path),
        "polygons": str(polygons_path),
        "steps": str(steps_path),
    }


# ---------------------------------------------------------------------------
# The pipeline itself
# ---------------------------------------------------------------------------
def build_baseline_flood_map(
    post_vv: np.ndarray,
    post_vh: np.ndarray,
    pre_vv: Sequence[np.ndarray],
    pre_vh: Sequence[np.ndarray],
    *,
    bbox: Sequence[float],
    transform: Any = None,
    slope_deg: Optional[np.ndarray] = None,
    hand_m: Optional[np.ndarray] = None,
    shadow_mask: Optional[np.ndarray] = None,
    orbit_direction: Optional[str] = None,
    scene_ids: Optional[Dict[str, Any]] = None,
    dem_meta: Optional[Dict[str, Any]] = None,
    config: Any = None,
) -> FloodMapResult:
    """Build the baseline flood map from same-grid dB arrays.

    Inputs must already share one grid (same shape): ``post_vv``/``post_vh`` and
    the pre-event stacks ``pre_vv``/``pre_vh``, with NaN marking nodata. Up to
    ``pairing.max_pre_scenes`` pre scenes are used; with a single pre scene no
    median is needed. Terrain inputs are optional: when ``slope_deg``/``hand_m``/
    ``shadow_mask`` are None the corresponding exclusion step is skipped and
    recorded as 0 pixels, and NaN terrain values are treated as unknown (not
    excluded) but counted.

    An input with no valid overlapping pixels returns an empty map (zero
    probability, empty mask, no polygons, area 0) with the counts explaining it:
    an all-nodata scene is not a flood, and refusing with an exception is the
    job of the pairing rule, not of this function.
    """
    cfg = config or app_config
    bbox = [float(c) for c in bbox]

    shapes = [np.asarray(arr).shape for arr in (post_vv, post_vh, *pre_vv, *pre_vh)]
    if len(set(shapes)) != 1:
        raise ValueError(
            f"pre/post clips must share one grid; got shapes {sorted(set(shapes))}"
        )
    if len(pre_vv) == 0 or len(pre_vh) == 0:
        raise ValueError("at least one pre-event scene is required")
    if len(pre_vv) != len(pre_vh):
        raise ValueError("pre_vv and pre_vh must contain the same number of scenes")
    if len(pre_vv) > cfg.pairing.max_pre_scenes:
        raise ValueError(
            f"got {len(pre_vv)} pre scenes but pairing.max_pre_scenes is "
            f"{cfg.pairing.max_pre_scenes}"
        )
    grid_shape = shapes[0]
    counts = {name: 0 for name in STEP_NAMES}
    counts["grid_pixels"] = int(np.prod(grid_shape))

    speckle_window = cfg.flood.speckle_filter_size
    filtered_post_vv = speckle_filter_db(post_vv, speckle_window, cfg)
    filtered_post_vh = speckle_filter_db(post_vh, speckle_window, cfg)
    filtered_pre_vv = [speckle_filter_db(band, speckle_window, cfg) for band in pre_vv]
    filtered_pre_vh = [speckle_filter_db(band, speckle_window, cfg) for band in pre_vh]

    # Median over up to N same-orbit pre scenes (a single scene stays as it is).
    reference_vv = median_pre_composite(filtered_pre_vv)
    reference_vh = median_pre_composite(filtered_pre_vh)

    valid = (
        np.isfinite(filtered_post_vv)
        & np.isfinite(filtered_post_vh)
        & np.isfinite(reference_vv)
        & np.isfinite(reference_vh)
    )
    counts["valid_overlap_pixels"] = int(valid.sum())

    provenance: Dict[str, Any] = {
        "bbox": bbox,
        "grid_shape": [int(grid_shape[0]), int(grid_shape[1])],
        "pre_scenes_used": len(pre_vv),
        "max_pre_scenes": int(cfg.pairing.max_pre_scenes),
        "median_pre": len(pre_vv) > 1,
        "speckle_filter_window": int(speckle_window),
        "polarization_weights": {
            "vv": float(cfg.flood.vv_weight),
            "vh": float(cfg.flood.vh_weight),
        },
        "orbit_direction": (orbit_direction or "").upper() or None,
        "scene_ids": scene_ids or {},
        "limitations": [
            "probability score is not calibrated",
            "orthorectified but not radiometrically terrain-corrected",
            "HAND is a proxy (no sink filling)",
        ],
    }
    if dem_meta:
        provenance["dem"] = dem_meta

    if counts["valid_overlap_pixels"] == 0:
        provenance["no_valid_overlap"] = True
        logger.warning("flood baseline: no valid pre/post overlap; returning an empty map")
        return FloodMapResult(
            probability=np.zeros(grid_shape, dtype="float32"),
            mask=np.zeros(grid_shape, dtype=bool),
            polygons={"type": "FeatureCollection", "features": []},
            area_km2=0.0,
            counts=counts,
            threshold_db=None,
            threshold_method="not_applicable",
            utm_epsg=geo.get_utm_epsg_for_bbox(bbox),
            crs="EPSG:4326",
            transform=transform,
            probability_raw=np.zeros(grid_shape, dtype="float32"),
            provenance=provenance,
        )

    ratio_vv = log_ratio_db(filtered_post_vv, reference_vv)
    ratio_vh = log_ratio_db(filtered_post_vh, reference_vh)
    combined = combine_polarizations(ratio_vv, ratio_vh, config=cfg)
    threshold, method = otsu_threshold(combined[valid], config=cfg)
    provenance["threshold_db"] = threshold
    provenance["threshold_method"] = method

    probability_raw = probability_from_log_ratio(combined, threshold, config=cfg)
    probability_raw = np.where(valid, probability_raw, np.nan).astype("float32")
    candidate = valid & (probability_raw >= cfg.flood.probability_threshold)
    counts["candidate_pixels"] = int(candidate.sum())

    # Exclusions: each removal is counted on its own so the drop is auditable.
    water = permanent_water_mask(reference_vv, reference_vh, config=cfg)
    candidate, removed = _drop(candidate, water)
    counts["excluded_permanent_water"] = removed

    slope_unknown = 0
    if slope_deg is not None:
        slope_arr = np.asarray(slope_deg, dtype="float64")
        _require_shape(slope_arr, grid_shape, "slope_deg")
        slope_unknown = int((~np.isfinite(slope_arr)).sum())
        slope_mask = slope_exclusion_mask(slope_arr, cfg.terrain.slope_cutoff_degrees)
        candidate, removed = _drop(candidate, slope_mask)
        counts["excluded_slope"] = removed

    shadow_unknown = 0
    if shadow_mask is not None:
        shadow_arr = np.asarray(shadow_mask)
        _require_shape(shadow_arr, grid_shape, "shadow_mask")
        shadow_bool = shadow_arr.astype(bool)
        candidate, removed = _drop(candidate, shadow_bool)
        counts["excluded_radar_shadow"] = removed

    hand_unknown = 0
    if hand_m is not None:
        hand_arr = np.asarray(hand_m, dtype="float64")
        _require_shape(hand_arr, grid_shape, "hand_m")
        hand_unknown = int((~np.isfinite(hand_arr)).sum())
        hand_mask = hand_exclusion_mask(hand_arr, cfg.terrain.hand_cutoff_m)
        candidate, removed = _drop(candidate, hand_mask)
        counts["excluded_hand"] = removed

    cleaned = clean_binary_mask(candidate, config=cfg)
    counts["excluded_small_components"] = int(candidate.sum()) - int(cleaned.sum())
    counts["final_mask_pixels"] = int(cleaned.sum())

    probability = np.where(cleaned, probability_raw, 0.0).astype("float32")
    probability = np.nan_to_num(probability, nan=0.0)

    polygons = (
        mask_to_geojson(cleaned, transform)
        if transform is not None
        else {"type": "FeatureCollection", "features": []}
    )
    area_km2, area_meta = mask_area_km2(cleaned, bbox)

    provenance["terrain_unknown_pixels"] = {
        "slope": slope_unknown,
        "hand": hand_unknown,
        "shadow": shadow_unknown,
    }
    provenance["area"] = area_meta
    provenance["no_valid_overlap"] = False

    return FloodMapResult(
        probability=probability,
        mask=cleaned,
        polygons=polygons,
        area_km2=area_km2,
        counts=counts,
        threshold_db=threshold,
        threshold_method=method,
        utm_epsg=int(area_meta["utm_epsg"]),
        crs="EPSG:4326",
        transform=transform,
        probability_raw=probability_raw,
        provenance=provenance,
    )


def _drop(candidate: np.ndarray, exclusion: np.ndarray) -> Tuple[np.ndarray, int]:
    """Remove excluded pixels, returning (remaining mask, pixels removed)."""
    kept = candidate & ~exclusion
    return kept, int(candidate.sum()) - int(kept.sum())


def _require_shape(arr: np.ndarray, shape: Tuple[int, int], name: str) -> None:
    if arr.shape != tuple(shape):
        raise ValueError(f"{name} must share the analysis grid {tuple(shape)}, got {arr.shape}")


# ---------------------------------------------------------------------------
# Network-facing wrapper (pairing rule first)
# ---------------------------------------------------------------------------
def fetch_and_build_flood_map(
    post_scene: Dict[str, Any],
    candidate_pre_scenes: Sequence[Dict[str, Any]],
    *,
    bbox: Sequence[float],
    resolution_m: Optional[float] = None,
    dem_window: Any = None,
    cache_root: Optional[str] = None,
    config: Any = None,
) -> FloodMapResult:
    """Enforce the same-orbit rule, fetch the clips, build the baseline map.

    Raises ``NoValidOrbitPairError`` (``NO_VALID_ORBIT_PAIR``) when no
    same-relative-orbit, same-direction pre scene exists: no raster, mask or
    polygon is produced in that case (PLAN.md G1 / .cursorrules rule 6).
    """
    cfg = config or app_config
    pairing = validate_and_pair_s1_scenes(
        post_scene, list(candidate_pre_scenes), max_pre_scenes=cfg.pairing.max_pre_scenes
    )
    resolution = resolution_m or cfg.fetch.resolution_m
    bbox = [float(c) for c in bbox]

    post_clip = fetch_s1_clip(bbox, post_scene, resolution_m=resolution, cache_root=cache_root)
    post_vv, post_vh = clip_band_pair(post_clip.data)
    post_grid = clip_grid(post_clip.data)

    pre_vv: List[np.ndarray] = []
    pre_vh: List[np.ndarray] = []
    pre_scene_ids: List[str] = []
    for pre in pairing.pre:
        scene = pre.model_dump()
        clip = fetch_s1_clip(bbox, scene, resolution_m=resolution, cache_root=cache_root)
        grid = clip_grid(clip.data)
        mismatch = grids_match(post_grid, grid)
        if mismatch:
            raise ValueError(
                f"pre clip {scene.get('scene_id')} does not share the post grid: {mismatch}"
            )
        vv, vh = clip_band_pair(clip.data)
        pre_vv.append(vv)
        pre_vh.append(vh)
        pre_scene_ids.append(str(scene.get("scene_id")))

    window = dem_window if dem_window is not None else dem.read_dem_window(bbox)
    analysis_transform = grid_transform(bbox, post_vv.shape)
    dem_transform = window.transform
    lat_centroid = (bbox[1] + bbox[3]) / 2.0
    x_m, y_m = metres_per_pixel(dem_transform, lat_centroid)
    dem_array = np.asarray(window.array, dtype="float64")
    dem_nodata = ~np.isfinite(dem_array)

    slope_dem = compute_slope_degrees(dem_array, x_m, y_m)
    hand_dem = compute_hand(
        dem_array, x_m=x_m, y_m=y_m, accumulation_threshold=cfg.upstream.accumulation_threshold
    )
    shadow_dem = compute_shadow_layover_mask(
        dem_array, x_m, y_m, radar_look_azimuth_deg(pairing.direction.value, cfg)
    )

    slope = resample_nearest(slope_dem, dem_transform, analysis_transform, post_vv.shape)
    hand = resample_nearest(hand_dem, dem_transform, analysis_transform, post_vv.shape)
    shadow = resample_nearest(
        shadow_dem.astype("float32"), dem_transform, analysis_transform, post_vv.shape
    ) > 0.5

    result = build_baseline_flood_map(
        post_vv,
        post_vh,
        pre_vv,
        pre_vh,
        bbox=bbox,
        transform=analysis_transform,
        slope_deg=slope,
        hand_m=hand,
        shadow_mask=shadow,
        orbit_direction=pairing.direction.value,
        scene_ids={
            "post": str(post_scene.get("scene_id")),
            "pre": pre_scene_ids,
            "relative_orbit": pairing.relative_orbit,
        },
        dem_meta={
            "source": "copernicus-dem-30m window",
            "from_cache": bool(getattr(window, "from_cache", False)),
            "shape": [int(dem_array.shape[0]), int(dem_array.shape[1])],
            "pixel_m": [round(x_m, 2), round(y_m, 2)],
            "nodata_pixels": int(dem_nodata.sum()),
            "hand_accumulation_threshold": int(cfg.upstream.accumulation_threshold),
            "look_azimuth_deg": radar_look_azimuth_deg(pairing.direction.value, cfg),
        },
        config=cfg,
    )
    result.provenance["grid"] = {
        "post": post_grid,
        "analysis_transform": list(analysis_transform)[:6],
        "resolution_m": resolution,
    }
    return result
