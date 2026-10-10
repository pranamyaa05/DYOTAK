"""Terrain conditioning and masking (Stage 4).

Computes slope, HAND (height above nearest drainage) and radar shadow/layover
masks from the windowed Copernicus DEM read (``app.pipeline.dem``).

Everything here is pure numpy on a north-up raster whose rows increase
southward and columns eastward; NaN marks nodata. The thresholds are read from
config (``terrain.*``) with the origin comments carried in
``config/default.yaml``.

Documented limitations (see docs/KNOWN_ISSUES.md):

* ``flow_accumulation_d8`` does **not** fill sinks/depressions and does not
  break flat areas, so the drainage network it derives is a proxy. Real HAND
  conditioning (priority-flood fill + stream burning) is not implemented.
* ``compute_shadow_layover_mask`` is a geometric proxy: it flags pixels whose
  single-sided slope in the radar range direction exceeds
  ``90 deg - incidence`` (``terrain.radar_shadow_slope_degrees``). A full SAR
  simulation (local incidence angle, layover overlap) is not implemented.
"""

from collections import deque
from typing import Any, Optional, Tuple
import math

import numpy as np

from app.settings import app_config

#: 8-connected neighbourhood as (row delta, col delta, step length in pixels).
D8_OFFSETS: Tuple[Tuple[int, int, float], ...] = (
    (-1, -1, math.sqrt(2.0)),
    (-1, 0, 1.0),
    (-1, 1, math.sqrt(2.0)),
    (0, -1, 1.0),
    (0, 1, 1.0),
    (1, -1, math.sqrt(2.0)),
    (1, 0, 1.0),
    (1, 1, math.sqrt(2.0)),
)


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------
def metres_per_pixel(transform: Any, latitude: float) -> Tuple[float, float]:
    """Ground spacing (x_m, y_m) of a north-up WGS84 grid at a latitude.

    Uses the local metres-per-degree approximation (no projection dependency);
    over a ~20 km AOI the error against a projected CRS is well below 1 %.
    """
    x_deg = abs(float(transform.a)) or 1.0
    y_deg = abs(float(transform.e)) or 1.0
    x_m = 111320.0 * math.cos(math.radians(latitude)) * x_deg
    y_m = 111320.0 * y_deg
    return x_m, y_m


# ---------------------------------------------------------------------------
# Slope
# ---------------------------------------------------------------------------
def compute_slope_degrees(dem: np.ndarray, x_m: float, y_m: float) -> np.ndarray:
    """Slope magnitude in degrees from the DEM (NaN where the DEM is nodata).

    Slope is the magnitude of the elevation gradient, so the sign convention of
    the row axis does not matter here. Pixels next to nodata are NaN because
    ``np.gradient`` propagates NaN through its stencil.
    """
    arr = np.asarray(dem, dtype="float64")
    dz_drow, dz_dcol = np.gradient(arr, y_m, x_m)
    return np.degrees(np.arctan(np.hypot(dz_dcol, dz_drow)))


def slope_exclusion_mask(slope_deg: np.ndarray, cutoff_deg: Optional[float] = None) -> np.ndarray:
    """True where the terrain is too steep for a trustworthy radar detection."""
    cutoff = float(
        cutoff_deg if cutoff_deg is not None else app_config.terrain.slope_cutoff_degrees
    )
    arr = np.asarray(slope_deg, dtype="float64")
    return np.isfinite(arr) & (arr > cutoff)


# ---------------------------------------------------------------------------
# Radar shadow / layover
# ---------------------------------------------------------------------------
def radar_look_azimuth_deg(orbit_direction: Optional[str], config: Any = None) -> float:
    """Range (look) azimuth of a right-looking Sentinel-1 IW pass.

    Ascending passes head roughly north, so the sensor looks east; descending
    passes head roughly south and look west. Values come from config
    (``flood.s1_look_azimuth_ascending_deg`` / ``_descending_deg``).
    """
    cfg = config or app_config
    direction = (orbit_direction or "").upper()
    if direction.startswith("ASC"):
        return float(cfg.flood.s1_look_azimuth_ascending_deg)
    return float(cfg.flood.s1_look_azimuth_descending_deg)


def compute_shadow_layover_mask(
    dem: np.ndarray,
    x_m: float,
    y_m: float,
    look_azimuth_deg: float,
    shadow_slope_deg: Optional[float] = None,
) -> np.ndarray:
    """Radar shadow/layover proxy for a single look direction.

    Projects the elevation gradient onto the range direction (looking toward
    ``look_azimuth_deg``) and flags pixels whose single-sided slope exceeds
    ``terrain.radar_shadow_slope_degrees``. A pixel sloping *away* from the
    sensor faster than that is in shadow; one sloping *toward* it is in
    layover. Both are unusable for change detection.
    """
    cutoff = float(
        shadow_slope_deg
        if shadow_slope_deg is not None
        else app_config.terrain.radar_shadow_slope_degrees
    )
    arr = np.asarray(dem, dtype="float64")
    # Row index increases southward, so the north component flips sign.
    grad_north = -np.gradient(arr, axis=0) / y_m
    grad_east = np.gradient(arr, axis=1) / x_m
    az = math.radians(look_azimuth_deg)
    toward_look = grad_east * math.sin(az) + grad_north * math.cos(az)
    return np.abs(toward_look) > math.tan(math.radians(cutoff))


# ---------------------------------------------------------------------------
# Drainage network + HAND
# ---------------------------------------------------------------------------
def flow_accumulation_d8(dem: np.ndarray, x_m: float = 30.0, y_m: float = 30.0) -> np.ndarray:
    """D8 flow accumulation: number of cells draining through each cell.

    Each cell is routed to its steepest descending neighbour, weighted by the
    real (metric) distance to that neighbour. Cells with no lower neighbour are
    local sinks or grid-edge outlets and accumulate only themselves. Sinks are
    NOT filled, which is the documented limitation of this proxy.
    """
    arr = np.asarray(dem, dtype="float64")
    if arr.ndim != 2:
        raise ValueError("flow_accumulation_d8 expects a 2-D array")
    height, width = arr.shape
    index = np.arange(height * width).reshape(height, width)
    receiver = np.full((height, width), -1, dtype=np.int64)
    best_drop = np.zeros((height, width), dtype="float64")

    for dy, dx, step in D8_OFFSETS:
        distance_m = math.hypot(dy * y_m, dx * x_m) or step
        row_lo, row_hi = max(0, -dy), min(height, height - dy)
        col_lo, col_hi = max(0, -dx), min(width, width - dx)
        if row_lo >= row_hi or col_lo >= col_hi:
            continue
        target = np.full((height, width), -1, dtype=np.int64)
        target[row_lo:row_hi, col_lo:col_hi] = index[
            row_lo + dy:row_hi + dy, col_lo + dx:col_hi + dx
        ]
        neighbour = np.full((height, width), np.nan, dtype="float64")
        neighbour[row_lo:row_hi, col_lo:col_hi] = arr[
            row_lo + dy:row_hi + dy, col_lo + dx:col_hi + dx
        ]
        drop = (arr - neighbour) / distance_m
        better = np.isfinite(drop) & (drop > best_drop)
        receiver[better] = target[better]
        best_drop[better] = drop[better]

    accumulation = np.ones(height * width, dtype=np.int64)
    flat_receiver = receiver.ravel()
    # Process cells from the highest to the lowest so every upstream cell has
    # already been accumulated when it flows into its receiver.
    order = np.argsort(arr.ravel(), kind="stable")[::-1]
    for cell in order:
        recv = flat_receiver[cell]
        if recv >= 0:
            accumulation[recv] += accumulation[cell]
    return accumulation.reshape(height, width)


def elevation_above_nearest_source(dem: np.ndarray, sources: np.ndarray) -> np.ndarray:
    """Elevation minus the elevation of the nearest source cell (8-connected BFS).

    ``sources`` is a boolean mask of drainage cells. Distances are measured in
    cells; each flooded/unknown cell inherits the elevation of the nearest
    drainage cell that reaches it first.
    """
    arr = np.asarray(dem, dtype="float64")
    src = np.asarray(sources, dtype=bool)
    if arr.shape != src.shape:
        raise ValueError("dem and sources must share one shape")
    height, width = arr.shape
    hand = np.full((height, width), np.nan, dtype="float64")
    source_elevation = np.full((height, width), np.nan, dtype="float64")

    queue: deque = deque()
    for row, col in zip(*np.nonzero(src & np.isfinite(arr))):
        hand[row, col] = 0.0
        source_elevation[row, col] = arr[row, col]
        queue.append((int(row), int(col)))

    while queue:
        row, col = queue.popleft()
        for dy, dx, _step in D8_OFFSETS:
            nr, nc = row + dy, col + dx
            if 0 <= nr < height and 0 <= nc < width and np.isnan(hand[nr, nc]):
                if not np.isfinite(arr[nr, nc]):
                    continue
                source_elevation[nr, nc] = source_elevation[row, col]
                hand[nr, nc] = arr[nr, nc] - source_elevation[nr, nc]
                queue.append((nr, nc))
    return hand


def compute_hand(
    dem: np.ndarray,
    accumulation_threshold: Optional[int] = None,
    x_m: float = 30.0,
    y_m: float = 30.0,
) -> np.ndarray:
    """Height above nearest drainage (proxy) for a DEM window.

    Drainage cells are those whose D8 flow accumulation reaches
    ``upstream.accumulation_threshold`` from config. Returns NaN where the DEM
    is nodata or when the window contains no drainage cell at all.
    """
    threshold = int(
        accumulation_threshold
        if accumulation_threshold is not None
        else app_config.upstream.accumulation_threshold
    )
    arr = np.asarray(dem, dtype="float64")
    accumulation = flow_accumulation_d8(arr, x_m=x_m, y_m=y_m)
    drainage = accumulation >= threshold
    if not (drainage & np.isfinite(arr)).any():
        # No river in this window: HAND is undefined rather than zero.
        return np.full(arr.shape, np.nan, dtype="float64")
    return elevation_above_nearest_source(arr, drainage)


def hand_exclusion_mask(hand_m: np.ndarray, cutoff_m: Optional[float] = None) -> np.ndarray:
    """True where the pixel sits too far above the drainage network."""
    cutoff = float(
        cutoff_m if cutoff_m is not None else app_config.terrain.hand_cutoff_m
    )
    arr = np.asarray(hand_m, dtype="float64")
    return np.isfinite(arr) & (arr > cutoff)


# ---------------------------------------------------------------------------
# Combined application (existing public API, kept)
# ---------------------------------------------------------------------------
def apply_terrain_masks(
    flood_prob: np.ndarray,
    slope: np.ndarray,
    hand: np.ndarray,
    slope_cutoff: float = 15.0,
    hand_cutoff: float = 15.0
) -> np.ndarray:
    """Mask out false positive flood detections on steep slopes or high HAND terrain."""
    filtered = np.copy(flood_prob)
    mask = (slope > slope_cutoff) | (hand > hand_cutoff)
    filtered[mask] = 0.0
    return filtered
