"""Damage overlay and classification (Stage 6, ARCHITECTURE.md Section 4.6).

Inputs are the filtered flood map (``flood_baseline.FloodMapResult``: probability
raster + grid transform) and the cached **ohsome v2 GeoParquet** extracts from
``ohsome.fetch_preevent_osm_elements`` (``parquet_paths``).

The parquet schema was inspected on the real cached files before this module was
written -- no column names are assumed:

============================  ====================================================
``osm_type``                  ``way`` / ``relation`` / ``node``
``osm_id``                    int64, stable OSM id
``tags``                      ``map<string,string>`` -> ``to_pylist`` yields
                              ``(key, value)`` pairs, read with ``dict()``
``geom_type``                 ``Polygon`` / ``MultiPolygon`` / ``LineString`` / ``Point``
``geom``                      WKB binary, ``geoarrow.wkb`` -> ``shapely.from_wkb``
``bbox``                      struct ``xmin/xmax/ymin/ymax`` (WGS84 degrees)
``clipped``                   bool, feature clipped by the AOI boundary
============================  ====================================================

Measured row counts for the Trishuli preset (2026-08-23 snapshot): building
60 024 (Polygon/MultiPolygon), highway 3 565 (LineString), bridge 102
(LineString, incl. aqueducts), amenity 6 (Point), place 93 (Point/Polygon).

Sampling (vectorised -- no Python loop over 60 k footprints):

* Every footprint gets a representative point (``shapely.point_on_surface``,
  vectorised). Footprint pixels are found by taking each footprint's pixel-space
  bounding box, batching those candidate pixel centres into flat arrays and
  testing them with one vectorised ``shapely.contains_xy`` call: a pixel belongs
  to a footprint when its centre is inside it (``all_touched=False``), which is
  exactly ``rasterio.features.rasterize``'s rule -- verified to produce
  identical per-footprint pixel counts and flooded fractions on all 60 024 live
  Trishuli footprints, while costing 0.03 s instead of 2.5-3.3 s (``rasterize``
  round-trips every polygon through WKT). Per-footprint pixel counts, flooded
  pixel counts and probability sums come from ``numpy.bincount``.
  ``rasterio.features.rasterize`` remains as the fallback for pathological
  inputs whose candidate set would be too large to batch (same numbers, see
  :func:`footprint_flood_stats`).
* ``flooded_fraction`` = flooded pixels / footprint pixels, where a pixel is
  flooded when its probability reaches ``flood.probability_threshold``;
  ``mean_probability`` is the probability-weighted overlap of the footprint.
* **Sub-pixel footprints.** At the 20 m fetch resolution a typical building is
  smaller than one pixel, so most footprints contain no pixel centre (measured
  live: 12.5 % of the 60 024 Trishuli buildings cover >= 1 pixel with
  ``all_touched=False``; ``all_touched=True`` would burn pixels shared with
  neighbouring buildings and bias the label raster). Those features fall back to
  a single representative-point sample and are marked ``sampled="centroid"``
  with a binary flooded fraction; their confidence tier is capped at ``medium``
  because a point sample is weaker evidence than a footprint fraction. A direct
  consequence on this AOI: the middle class can be empty (a sub-pixel building
  is flooded in its pixel or it is not).

``status`` (fractions from config) and a ``confidence`` tier (distance to the
nearest status boundary) are attached to every feature. Bridges are only ever
``possibly_impacted`` or ``unaffected`` -- :data:`BRIDGE_STATUSES` has no
"destroyed" on purpose: radar change detection observes backscatter, not
structural integrity.

Everything metric (road lengths, bridge buffers) is computed in the UTM zone of
the AOI centroid (``app.common.geo.get_utm_epsg_for_bbox``, never hardcoded).

Per-building results are kept as numpy arrays (``building_status_codes``,
``building_points``, ...) instead of 60 k Python dicts: the dicts made CPython's
collector dominate the stage runtime (measured 13-18 s versus 4-5 s with the same
work and a smaller object graph). ``building_records`` therefore carries the
``affected`` / ``possibly_affected`` features only.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.common import geo
from app.settings import app_config

logger = logging.getLogger("dyotak.damage")

#: Statuses a building or road feature may carry (ARCHITECTURE 4.6).
FEATURE_STATUSES: Tuple[str, ...] = ("affected", "possibly_affected", "unaffected")
#: Statuses a bridge may carry. "destroyed" is deliberately absent.
BRIDGE_STATUSES: Tuple[str, ...] = ("possibly_impacted", "unaffected")
#: Confidence tiers.
CONFIDENCE_TIERS: Tuple[str, ...] = ("high", "medium", "low")
#: How a feature's flood evidence was obtained.
SAMPLING_MODES: Tuple[str, ...] = ("footprint", "centroid")
#: Bridge subtypes: the ohsome bridge extract also holds non-road structures
#: (measured: 3 of 102 Trishuli rows are ``bridge=aqueduct`` without a highway).
BRIDGE_STRUCTURES: Tuple[str, ...] = ("road_bridge", "other_bridge")

#: Status code -> label. 0 is "no flood evidence".
STATUS_BY_CODE: Tuple[str, ...] = ("unaffected", "possibly_affected", "affected")
#: Confidence code -> label.
CONFIDENCE_BY_CODE: Tuple[str, ...] = ("low", "medium", "high")

#: Columns read from the GeoParquet extract. The timestamp/user columns are
#: skipped on purpose: a tz-aware timestamp column needs zoneinfo/pytz to
#: convert, and this stage never uses them (the extract is already pinned to the
#: pre-event snapshot by ``ohsome.fetch_preevent_osm_elements``).
_LAYER_COLUMNS = ("osm_type", "osm_id", "tags", "geom_type", "geom", "clipped")

#: Upper bound on the number of batched pixel-centre candidates in
#: :func:`footprint_flood_stats` before it falls back to ``rasterio.rasterize``.
#: The live Trishuli run needs 124 553 candidates for 60 024 buildings
#: (max 48 per footprint), so the fast path covers realistic inputs by a wide
#: margin; the cap only guards a pathological AOI-wide footprint.
MAX_FOOTPRINT_CANDIDATES = 8_000_000


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------
@dataclass
class OSMLayer:
    """One ohsome v2 extract decoded from GeoParquet into WGS84 geometries."""

    feature_type: str
    path: str
    osm_type: List[str]
    osm_id: List[int]
    tags: List[Dict[str, str]]
    geometries: np.ndarray              # shapely geometries (object array), EPSG:4326
    geom_types: List[str]
    clipped: List[bool]
    dropped_geometries: int = 0         # null/empty geometry rows skipped at read time

    def __len__(self) -> int:
        return int(self.geometries.shape[0])

    def matching_tags(
        self, keys: Iterable[str], values: Optional[Iterable[str]] = None
    ) -> List[int]:
        """Indices of features carrying one of ``keys``.

        ``values`` restricts the match to those tag values (e.g. ``place`` in
        ``{"town", "village"}``).
        """
        wanted = set(keys)
        accepted = None if values is None else {str(v) for v in values}
        hits: List[int] = []
        for index, tag in enumerate(self.tags):
            for key in wanted:
                if key not in tag:
                    continue
                if accepted is None or tag.get(key) in accepted:
                    hits.append(index)
                    break
        return hits

    def tag_value(self, index: int, key: str) -> Optional[str]:
        return self.tags[index].get(key)


@dataclass
class FootprintStats:
    """Per-footprint flood statistics on the flood-map grid."""

    flooded_fraction: np.ndarray        # float64, [0, 1]
    mean_probability: np.ndarray        # float64, probability-weighted overlap
    pixel_count: np.ndarray             # int64 footprint pixels on the raster grid
    sampled_centroid: np.ndarray        # bool, True where the point fallback ran
    points_lon: np.ndarray              # float64 representative point (WGS84)
    points_lat: np.ndarray


@dataclass
class DamageResult:
    """Classified damage layers plus the numeric facts for facts.json."""

    buildings: Dict[str, Any]                 # GeoJSON FeatureCollection (non-unaffected)
    roads: Dict[str, Any]                     # GeoJSON FeatureCollection (non-unaffected)
    bridges: Dict[str, Any]                   # GeoJSON FeatureCollection (all bridges)
    counts: Dict[str, Any]
    facts: List[Any]                          # contracts.schemas.FactItem
    timings: Dict[str, float]
    utm_epsg: int
    building_records: List[Dict[str, Any]] = field(default_factory=list)
    building_status_codes: Optional[np.ndarray] = None   # (N,) int8, see STATUS_BY_CODE
    building_confidence_codes: Optional[np.ndarray] = None
    building_flooded_fraction: Optional[np.ndarray] = None
    building_osm_ids: Optional[np.ndarray] = None
    building_points: Optional[np.ndarray] = None         # (N, 2) WGS84 representative points
    road_records: List[Dict[str, Any]] = field(default_factory=list)
    bridge_records: List[Dict[str, Any]] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    crs: str = "EPSG:4326"

    def affected_building_points(self) -> np.ndarray:
        """WGS84 representative points of the ``affected`` buildings (for Stage 7)."""
        if self.building_status_codes is None or self.building_points is None:
            return np.zeros((0, 2), dtype="float64")
        return self.building_points[self.building_status_codes == 2]


# ---------------------------------------------------------------------------
# GeoParquet reading (real schema)
# ---------------------------------------------------------------------------
def read_osm_layer(path: str | Path, feature_type: str) -> OSMLayer:
    """Decode one ohsome v2 GeoParquet extract into WGS84 shapely geometries.

    Rows whose geometry is null or empty are skipped and counted in
    ``dropped_geometries`` (all parallel arrays stay aligned).
    """
    import pyarrow.parquet as pq
    import shapely
    from shapely import from_wkb

    table = pq.read_table(str(path), columns=list(_LAYER_COLUMNS))
    geometries = from_wkb(table["geom"].to_numpy(zero_copy_only=False))
    # A null WKB decodes to None; is_valid_input accepts None, so it is filtered
    # explicitly (a row without geometry cannot be sampled or counted).
    keep = (
        ~shapely.is_missing(geometries)
        & shapely.is_valid_input(geometries)
        & ~shapely.is_empty(geometries)
    )
    dropped = int((~keep).sum())
    if dropped:
        logger.warning(
            "damage: %s has %d rows without a usable geometry", feature_type, dropped
        )

    return OSMLayer(
        feature_type=feature_type,
        path=str(path),
        osm_type=[str(v) for v in np.asarray(table["osm_type"].to_pylist(), dtype=object)[keep]],
        osm_id=[int(v) for v in np.asarray(table["osm_id"].to_pylist(), dtype="int64")[keep]],
        tags=[dict(pairs) for pairs in np.asarray(table["tags"].to_pylist(), dtype=object)[keep]],
        geometries=geometries[keep],
        geom_types=[str(v) for v in np.asarray(table["geom_type"].to_pylist(), dtype=object)[keep]],
        clipped=[bool(v) for v in np.asarray(table["clipped"].to_pylist(), dtype=bool)[keep]],
        dropped_geometries=dropped,
    )


def read_osm_layers(parquet_paths: Dict[str, str]) -> Dict[str, OSMLayer]:
    """Decode every feature type present in ``parquet_paths``."""
    layers: Dict[str, OSMLayer] = {}
    for feature_type, path in (parquet_paths or {}).items():
        if path is None or not Path(path).is_file():
            logger.warning("damage: missing parquet for %s (%s)", feature_type, path)
            continue
        layers[str(feature_type)] = read_osm_layer(path, str(feature_type))
    return layers


# ---------------------------------------------------------------------------
# Grid / CRS helpers
# ---------------------------------------------------------------------------
def flood_grid(flood) -> Tuple[np.ndarray, Any]:
    """Probability raster (float64) and grid transform of a flood result.

    ``build_baseline_flood_map`` can be called without a transform; the grid is
    then rebuilt from the bbox in provenance, using the same helper the flood
    stage uses for its own artifacts.
    """
    from app.pipeline.flood_baseline import grid_transform

    probability = np.asarray(getattr(flood, "probability", None), dtype="float64")
    if probability.ndim != 2 or probability.size == 0:
        raise ValueError("flood result must carry a 2-D probability raster")
    transform = getattr(flood, "transform", None)
    if transform is None:
        bbox = (getattr(flood, "provenance", None) or {}).get("bbox")
        if not bbox:
            raise ValueError("flood result has neither a grid transform nor a bbox")
        transform = grid_transform(bbox, probability.shape)
    return probability, transform


def flood_bbox(flood, transform: Any, shape: Tuple[int, int]) -> List[float]:
    """AOI bbox from the flood provenance, else derived from the grid."""
    bbox = (getattr(flood, "provenance", None) or {}).get("bbox")
    if bbox:
        return [float(c) for c in bbox]
    height, width = int(shape[0]), int(shape[1])
    left, top = float(transform.c), float(transform.f)
    right = left + width * float(transform.a)
    bottom = top + height * float(transform.e)
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def project_to_utm(bbox: Sequence[float]) -> Tuple[int, Any, Any]:
    """UTM zone of the AOI centroid, plus 4326->UTM and UTM->4326 callables."""
    from rasterio.warp import transform as warp_transform

    epsg = int(geo.get_utm_epsg_for_bbox([float(c) for c in bbox]))

    def _make(source: str, target: str):
        def project(x, y):
            out_x, out_y = warp_transform(
                source, target, np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64")
            )
            return np.asarray(out_x, dtype="float64"), np.asarray(out_y, dtype="float64")

        return project

    return epsg, _make("EPSG:4326", f"EPSG:{epsg}"), _make(f"EPSG:{epsg}", "EPSG:4326")


def project_geometries(geometries: Iterable[Any], project) -> List[Any]:
    """Project shapely geometries one by one (``shapely.ops.transform``)."""
    from shapely.ops import transform as shapely_transform

    return [shapely_transform(project, geom) for geom in geometries]


def project_linestrings(geometries: Sequence[Any], project) -> np.ndarray:
    """Project polylines with one coordinate batch (no per-geometry WKT round-trip).

    Multi-part geometries fall back to the per-geometry path, so the result keeps
    the input order and length either way.
    """
    import shapely

    geoms = np.asarray(geometries, dtype=object)
    if geoms.shape[0] == 0:
        return geoms
    if not np.all(shapely.get_type_id(geoms) == 1):  # 1 == LineString
        return np.asarray(project_geometries(geoms, project), dtype=object)

    counts = shapely.get_num_coordinates(geoms)
    if (counts < 2).any():
        return np.asarray(project_geometries(geoms, project), dtype=object)
    coordinates = shapely.get_coordinates(geoms)
    x, y = project(coordinates[:, 0], coordinates[:, 1])
    indices = np.repeat(np.arange(geoms.shape[0], dtype="int64"), counts)
    return shapely.linestrings(x, y, indices=indices)


# ---------------------------------------------------------------------------
# Raster sampling (vectorised)
# ---------------------------------------------------------------------------
def rasterize_features(
    geometries: Sequence[Any], transform: Any, shape: Tuple[int, int], all_touched: bool = False
) -> np.ndarray:
    """Label raster: pixel value = 1-based feature index, 0 = background."""
    from rasterio.features import rasterize

    geoms = np.asarray(geometries, dtype=object)
    shapes = ((geom, index + 1) for index, geom in enumerate(geoms))
    return rasterize(
        shapes,
        out_shape=(int(shape[0]), int(shape[1])),
        transform=transform,
        fill=0,
        all_touched=all_touched,
        dtype="int32",
    )


def sample_raster_points(
    raster: np.ndarray, transform: Any, xs: np.ndarray, ys: np.ndarray
) -> np.ndarray:
    """Nearest-pixel lookup of raster values at WGS84 coordinates (NaN outside)."""
    arr = np.asarray(raster)
    height, width = arr.shape
    xs = np.asarray(xs, dtype="float64")
    ys = np.asarray(ys, dtype="float64")
    cols = np.floor((xs - float(transform.c)) / float(transform.a)).astype("int64")
    rows = np.floor((ys - float(transform.f)) / float(transform.e)).astype("int64")
    inside = (cols >= 0) & (cols < width) & (rows >= 0) & (rows < height)
    values = np.full(xs.shape, np.nan, dtype="float64")
    if inside.any():
        values[inside] = arr[rows[inside], cols[inside]]
    return values


def _bincount1d(values: np.ndarray, weights: Optional[np.ndarray], size: int) -> np.ndarray:
    """``numpy.bincount`` over 1-based raster labels, re-indexed to 0-based features.

    ``rasterize_features`` labels feature ``i`` with ``i + 1``; slot ``k`` of the
    result is the aggregate for feature ``k`` and slot 0 (background) is dropped.
    """
    counts = np.bincount(np.asarray(values, dtype="int64"), weights=weights, minlength=size + 1)
    return np.asarray(counts[1: size + 1], dtype="float64")


def pixel_centre_candidates(
    geometries: np.ndarray, transform: Any, shape: Tuple[int, int]
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Flat candidate pixel centres per footprint, from its pixel-space bbox.

    Returns ``(feature_index, col, row, candidate_count_per_feature)`` where the
    first three arrays have one entry per candidate pixel centre (clipped to the
    raster). Every pixel whose centre lies inside a footprint is guaranteed to be
    in that footprint's candidate set, because the candidate set is the bbox.
    """
    import shapely

    bounds = shapely.bounds(geometries)                      # (N, 4) minx, miny, maxx, maxy
    height, width = int(shape[0]), int(shape[1])
    a, e = float(transform.a), float(transform.e)
    c, f = float(transform.c), float(transform.f)
    min_col = np.clip(np.floor((bounds[:, 0] - c) / a), 0, width - 1).astype("int64")
    max_col = np.clip(np.floor((bounds[:, 2] - c) / a), 0, width - 1).astype("int64")
    min_row = np.clip(np.floor((bounds[:, 3] - f) / e), 0, height - 1).astype("int64")
    max_row = np.clip(np.floor((bounds[:, 1] - f) / e), 0, height - 1).astype("int64")
    widths = max_col - min_col + 1
    counts = widths * (max_row - min_row + 1)

    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    index = np.repeat(np.arange(counts.shape[0], dtype="int64"), counts)
    within = np.arange(int(counts.sum()), dtype="int64") - np.repeat(starts, counts)
    row_width = widths[index]
    col = min_col[index] + (within % row_width)
    row = min_row[index] + (within // row_width)
    return index, col, row, counts


def _footprint_stats_from_arrays(
    index: np.ndarray,
    col: np.ndarray,
    row: np.ndarray,
    keep: np.ndarray,
    probability: np.ndarray,
    features: int,
    flooded_threshold: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bincount the pixel membership into (pixel_count, flooded_pixels, probability_sum)."""
    if not keep.any():
        zeros = np.zeros(features, dtype="float64")
        return np.zeros(features, dtype="int64"), zeros, zeros.copy()
    owner = index[keep]
    rows = row[keep]
    cols = col[keep]
    values = probability[rows, cols]
    pixel_count = np.bincount(owner, minlength=features)
    flooded_pixels = np.bincount(
        owner[values >= float(flooded_threshold)], minlength=features
    ).astype("float64")
    probability_sum = np.bincount(owner, weights=values, minlength=features)
    return pixel_count.astype("int64"), flooded_pixels, probability_sum


def footprint_flood_stats(
    geometries: Sequence[Any],
    probability: np.ndarray,
    transform: Any,
    flooded_threshold: float,
    max_candidates: int = MAX_FOOTPRINT_CANDIDATES,
) -> FootprintStats:
    """Flooded fraction / mean probability per footprint, vectorised.

    Pixels are assigned to a footprint when their centre is inside it (the same
    rule as ``rasterio.features.rasterize(all_touched=False)``), computed with one
    batched ``shapely.contains_xy`` call over each footprint's pixel-space bbox.
    Above ``max_candidates`` batched candidates the label-raster implementation is
    used instead (identical numbers, bounded memory).

    Footprints covering no pixel centre -- sub-pixel footprints at the fetch
    resolution -- are sampled at one representative point instead and flagged in
    ``sampled_centroid``.
    """
    import shapely

    geoms = np.asarray(geometries, dtype=object)
    features = int(geoms.shape[0])
    probability = np.asarray(probability, dtype="float64")
    if features == 0:
        empty = np.zeros(0, dtype="float64")
        return FootprintStats(
            flooded_fraction=empty,
            mean_probability=empty.copy(),
            pixel_count=np.zeros(0, dtype="int64"),
            sampled_centroid=np.zeros(0, dtype=bool),
            points_lon=empty.copy(),
            points_lat=empty.copy(),
        )

    points = shapely.point_on_surface(geoms)
    points_lon = np.asarray(shapely.get_x(points), dtype="float64")
    points_lat = np.asarray(shapely.get_y(points), dtype="float64")
    del points

    index, col, row, counts = pixel_centre_candidates(geoms, transform, probability.shape)
    if int(counts.sum()) > int(max_candidates):
        pixel_count, flooded_pixels, probability_sum = _footprint_stats_from_raster(
            geoms, probability, transform, flooded_threshold, features
        )
    else:
        x = float(transform.c) + (col + 0.5) * float(transform.a)
        y = float(transform.f) + (row + 0.5) * float(transform.e)
        inside = shapely.contains_xy(geoms[index], x, y)
        pixel_count, flooded_pixels, probability_sum = _footprint_stats_from_arrays(
            index, col, row, inside, probability, features, flooded_threshold
        )

    covered = pixel_count > 0
    flooded_fraction = np.zeros(features, dtype="float64")
    mean_probability = np.zeros(features, dtype="float64")
    flooded_fraction[covered] = flooded_pixels[covered] / pixel_count[covered]
    mean_probability[covered] = probability_sum[covered] / pixel_count[covered]

    sampled_centroid = ~covered
    if sampled_centroid.any():
        sub = np.nonzero(sampled_centroid)[0]
        values = sample_raster_points(probability, transform, points_lon[sub], points_lat[sub])
        values = np.where(np.isfinite(values), values, 0.0)
        flooded_fraction[sub] = np.where(values >= float(flooded_threshold), 1.0, 0.0)
        mean_probability[sub] = values

    return FootprintStats(
        flooded_fraction=flooded_fraction,
        mean_probability=mean_probability,
        pixel_count=pixel_count,
        sampled_centroid=sampled_centroid,
        points_lon=points_lon,
        points_lat=points_lat,
    )


def _footprint_stats_from_raster(
    geoms: np.ndarray,
    probability: np.ndarray,
    transform: Any,
    flooded_threshold: float,
    features: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Label-raster fallback for footprints with an oversized candidate set."""
    labels = rasterize_features(geoms, transform, probability.shape)
    flat = labels.ravel()
    pixel_count = _bincount1d(flat, None, features).astype("int64")
    flooded_mask = probability >= float(flooded_threshold)
    flooded_pixels = _bincount1d(labels[flooded_mask].ravel(), None, features)
    probability_sum = _bincount1d(flat, probability.ravel(), features)
    return pixel_count, flooded_pixels, probability_sum


def sample_lines_at_spacing(
    geometries_utm: Sequence[Any], spacing_m: float
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample every polyline in UTM every ``spacing_m`` metres, in pure numpy.

    Returns ``(xs, ys, owner, spacing, lengths_m)`` with one entry per sample:
    ``owner`` is the feature index, ``spacing`` the distance to the next sample
    on the same feature (0 for the last one, so the caller can weight by length)
    and ``lengths_m`` the polyline length in metres. Working on the coordinate
    arrays avoids one shapely Point object per sample, which matters at this
    scale (measured: 95 417 road samples on the Trishuli AOI).
    """
    import shapely

    geoms = np.asarray(geometries_utm, dtype=object)
    features = int(geoms.shape[0])
    xs: List[np.ndarray] = []
    ys: List[np.ndarray] = []
    owner: List[np.ndarray] = []
    spacing: List[np.ndarray] = []
    lengths = np.zeros(features, dtype="float64")
    if features == 0:
        empty = np.zeros(0, dtype="float64")
        return empty, empty, np.zeros(0, dtype="int64"), empty, lengths

    coordinates = shapely.get_coordinates(geoms)
    counts = shapely.get_num_coordinates(geoms)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    step_m = float(spacing_m)
    for index in range(features):
        segment = coordinates[starts[index]: starts[index] + counts[index]]
        if segment.shape[0] < 2:
            continue
        deltas = np.hypot(np.diff(segment[:, 0]), np.diff(segment[:, 1]))
        cumulative = np.concatenate([[0.0], np.cumsum(deltas)])
        total = float(cumulative[-1])
        lengths[index] = total
        if total <= 0.0:
            continue
        steps = max(2, int(np.ceil(total / step_m)) + 1)
        targets = np.linspace(0.0, total, steps)
        upper = np.clip(np.searchsorted(cumulative, targets, side="right") - 1, 0, cumulative.shape[0] - 2)
        segment_length = deltas[upper]
        fraction = np.where(segment_length > 0, (targets - cumulative[upper]) / segment_length, 0.0)
        xs.append(segment[upper, 0] + fraction * (segment[upper + 1, 0] - segment[upper, 0]))
        ys.append(segment[upper, 1] + fraction * (segment[upper + 1, 1] - segment[upper, 1]))
        owner.append(np.full(steps, index, dtype="int64"))
        spacing.append(np.append(np.diff(targets), 0.0))

    if not xs:
        empty = np.zeros(0, dtype="float64")
        return empty, empty, np.zeros(0, dtype="int64"), empty, lengths
    return (
        np.concatenate(xs),
        np.concatenate(ys),
        np.concatenate(owner),
        np.concatenate(spacing),
        lengths,
    )


def line_sample_coordinates(
    geometries_utm: Sequence[Any],
    spacing_m: float,
    to_wgs84=None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample polylines every ``spacing_m`` (UTM) and optionally project to WGS84.

    Thin wrapper over :func:`sample_lines_at_spacing` that keeps the historical
    four-value return used by callers that only need sample coordinates.
    """
    xs, ys, owner, spacing, _ = sample_lines_at_spacing(geometries_utm, spacing_m)
    if to_wgs84 is not None and xs.shape[0]:
        xs, ys = to_wgs84(xs, ys)
    return np.asarray(xs, dtype="float64"), np.asarray(ys, dtype="float64"), owner, spacing


def aggregate_edge_probability(
    sampled_values: np.ndarray,
    owner: np.ndarray,
    spacing: np.ndarray,
    features: int,
    flooded_threshold: float,
    weights: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Length-weighted mean probability, max probability and flooded share per edge.

    Returns ``(mean_probability, max_probability, flooded_fraction)``. Samples
    outside the raster (NaN) are ignored; an edge without a usable sample gets
    ``NaN``, which callers read as "no evidence".
    """
    values = np.asarray(sampled_values, dtype="float64")
    owner = np.asarray(owner, dtype="int64")
    mean = np.full(features, np.nan, dtype="float64")
    maximum = np.full(features, np.nan, dtype="float64")
    flooded_fraction = np.zeros(features, dtype="float64")
    if values.shape[0] == 0 or features == 0:
        return mean, maximum, flooded_fraction

    if weights is None:
        spacing = np.asarray(spacing, dtype="float64")
        weights = np.where(spacing > 0.0, spacing, 1.0)
    else:
        weights = np.asarray(weights, dtype="float64")
    usable = np.isfinite(values)
    weighted_sum = np.bincount(owner[usable], weights=(values * weights)[usable], minlength=features)
    weight_sum = np.bincount(owner[usable], weights=weights[usable], minlength=features)
    has = weight_sum > 0
    mean[has] = weighted_sum[has] / weight_sum[has]

    maxima = np.full(features, -np.inf, dtype="float64")
    np.maximum.at(maxima, owner[usable], values[usable])
    finite = np.isfinite(maxima)
    maximum[finite] = maxima[finite]

    total_samples = np.bincount(owner, minlength=features).astype("float64")
    flooded = np.bincount(owner[usable & (values >= float(flooded_threshold))], minlength=features)
    present = total_samples > 0
    flooded_fraction[present] = flooded[present] / total_samples[present]
    return mean, maximum, flooded_fraction


# ---------------------------------------------------------------------------
# Classification rules (scalar wrappers over the vectorised core)
# ---------------------------------------------------------------------------
def _fractions(
    config: Any, affected_fraction: Optional[float], possibly_fraction: Optional[float]
) -> Tuple[float, float]:
    cfg = config or app_config
    affected = float(
        affected_fraction if affected_fraction is not None else cfg.damage.affected_overlap_fraction
    )
    possibly = float(
        possibly_fraction
        if possibly_fraction is not None
        else cfg.damage.possibly_affected_overlap_fraction
    )
    return affected, possibly


def status_codes(
    flooded_fraction: np.ndarray,
    affected_fraction: float,
    possibly_fraction: float,
) -> np.ndarray:
    """Vectorised status: 0 unaffected, 1 possibly_affected, 2 affected."""
    fractions = np.asarray(flooded_fraction, dtype="float64")
    codes = np.zeros(fractions.shape, dtype="int8")
    codes[fractions >= float(possibly_fraction)] = 1
    codes[fractions >= float(affected_fraction)] = 2
    return codes


def confidence_codes(
    flooded_fraction: np.ndarray,
    affected_fraction: float,
    possibly_fraction: float,
    high_margin: float,
    medium_margin: float,
    sampled_centroid: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Vectorised confidence tier: 0 low, 1 medium, 2 high.

    ``high`` at exactly 0 (nothing flooded at all) or at least ``high_margin``
    away from either status boundary, ``medium`` above ``medium_margin``, else
    ``low`` (the footprint straddles a boundary). Centroid-sampled features are
    capped at ``medium``.
    """
    fractions = np.asarray(flooded_fraction, dtype="float64")
    if fractions.shape[0] == 0:
        return np.zeros(0, dtype="int8")
    margin = np.minimum(
        np.abs(fractions - float(possibly_fraction)), np.abs(fractions - float(affected_fraction))
    )
    codes = np.zeros(fractions.shape, dtype="int8")
    codes[margin >= float(medium_margin)] = 1
    codes[(fractions == 0.0) | (margin >= float(high_margin))] = 2
    if sampled_centroid is not None:
        capped = np.asarray(sampled_centroid, dtype=bool) & (codes == 2)
        codes[capped] = 1
    return codes


def classify_status(
    flooded_fraction: float,
    affected_fraction: Optional[float] = None,
    possibly_fraction: Optional[float] = None,
    config: Any = None,
) -> str:
    """affected / possibly_affected / unaffected for one flooded fraction."""
    affected, possibly = _fractions(config, affected_fraction, possibly_fraction)
    code = int(status_codes(np.asarray([flooded_fraction]), affected, possibly)[0])
    return STATUS_BY_CODE[code]


def confidence_tier(
    flooded_fraction: float,
    affected_fraction: Optional[float] = None,
    possibly_fraction: Optional[float] = None,
    sampled_centroid: bool = False,
    config: Any = None,
) -> str:
    """Confidence tier for one flooded fraction (see :func:`confidence_codes`)."""
    cfg = config or app_config
    affected, possibly = _fractions(config, affected_fraction, possibly_fraction)
    code = int(
        confidence_codes(
            np.asarray([flooded_fraction]),
            affected,
            possibly,
            float(cfg.damage.confidence_high_margin),
            float(cfg.damage.confidence_medium_margin),
            np.asarray([sampled_centroid], dtype=bool),
        )[0]
    )
    return CONFIDENCE_BY_CODE[code]


def classify_bridge_status(
    flooded_fraction: float,
    possibly_fraction: Optional[float] = None,
    config: Any = None,
) -> str:
    """Bridges are ``possibly_impacted`` or ``unaffected`` -- never "destroyed"."""
    _, possibly = _fractions(config, None, possibly_fraction)
    status = "possibly_impacted" if flooded_fraction >= possibly else "unaffected"
    assert status in BRIDGE_STATUSES  # guard: no structural claim is ever made
    return status


def bridge_structure(tags: Dict[str, str]) -> str:
    """``road_bridge`` when the structure carries a highway, else ``other_bridge``."""
    return "road_bridge" if tags.get("highway") else "other_bridge"


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------
def classify_damage(flood, parquet_paths: Dict[str, str], config: Any = None) -> DamageResult:
    """Classify buildings, roads and bridges against the filtered flood map.

    ``flood`` is a ``flood_baseline.FloodMapResult``; ``parquet_paths`` is the
    mapping returned by ``ohsome.fetch_preevent_osm_elements``. Buildings and
    highways are required; a missing bridge layer degrades with a recorded
    limitation instead of failing.
    """
    cfg = config or app_config
    started = time.time()
    timings: Dict[str, float] = {}
    limitations: List[str] = []

    probability, transform = flood_grid(flood)
    bbox = flood_bbox(flood, transform, probability.shape)
    flooded_threshold = float(cfg.flood.probability_threshold)
    affected_fraction = float(cfg.damage.affected_overlap_fraction)
    possibly_fraction = float(cfg.damage.possibly_affected_overlap_fraction)
    high_margin = float(cfg.damage.confidence_high_margin)
    medium_margin = float(cfg.damage.confidence_medium_margin)
    utm_epsg, to_utm, to_wgs = project_to_utm(bbox)

    mark = time.time()
    layers = read_osm_layers(parquet_paths)
    timings["read_parquet_s"] = round(time.time() - mark, 3)
    if "building" not in layers:
        raise ValueError("the building extract is required for Stage 6 (damage)")
    if "highway" not in layers:
        limitations.append("no highway extract: road damage was not computed")
    if "bridge" not in layers:
        limitations.append("no bridge extract: bridge status was not computed")

    # --- buildings ---------------------------------------------------------
    mark = time.time()
    buildings = layers["building"]
    stats = footprint_flood_stats(buildings.geometries, probability, transform, flooded_threshold)
    codes = status_codes(stats.flooded_fraction, affected_fraction, possibly_fraction)
    tiers = confidence_codes(
        stats.flooded_fraction,
        affected_fraction,
        possibly_fraction,
        high_margin,
        medium_margin,
        stats.sampled_centroid,
    )
    affected_indices = np.nonzero(codes)[0]
    building_records = [
        {
            "osm_id": buildings.osm_id[index],
            "osm_type": buildings.osm_type[index],
            "status": STATUS_BY_CODE[int(codes[index])],
            "confidence": CONFIDENCE_BY_CODE[int(tiers[index])],
            "flooded_fraction": round(float(stats.flooded_fraction[index]), 4),
            "mean_probability": round(float(stats.mean_probability[index]), 4),
            "pixels": int(stats.pixel_count[index]),
            "clipped": buildings.clipped[index],
            "sampled": "centroid" if stats.sampled_centroid[index] else "footprint",
        }
        for index in affected_indices
    ]
    building_geojson = _collection(
        [
            _feature(record, buildings.geometries[index], _BUILDING_PROPERTIES)
            for record, index in zip(building_records, affected_indices)
        ]
    )
    timings["buildings_s"] = round(time.time() - mark, 3)
    centroid_share = float(stats.sampled_centroid.mean()) if len(buildings) else 0.0
    if centroid_share:
        limitations.append(
            f"{centroid_share * 100:.1f}% of {len(buildings)} building footprints are smaller "
            "than one flood-map pixel and were sampled at a representative point "
            "(confidence capped at medium)"
        )

    # --- roads -------------------------------------------------------------
    mark = time.time()
    road_records: List[Dict[str, Any]] = []
    highways = layers.get("highway")
    if highways is not None and len(highways):
        utm_lines = project_linestrings(highways.geometries, to_utm)
        xs, ys, owner, spacing, lengths_m = sample_lines_at_spacing(
            utm_lines, float(cfg.damage.road_sample_spacing_m)
        )
        xs_wgs, ys_wgs = to_wgs(xs, ys) if xs.shape[0] else (xs, ys)
        values = sample_raster_points(probability, transform, xs_wgs, ys_wgs)
        means, maxima, fractions = aggregate_edge_probability(
            values, owner, spacing, len(highways), flooded_threshold
        )
        road_codes = status_codes(fractions, affected_fraction, possibly_fraction)
        road_tiers = confidence_codes(
            fractions, affected_fraction, possibly_fraction, high_margin, medium_margin
        )
        for index in range(len(highways)):
            length_km = float(lengths_m[index]) / 1000.0
            road_records.append(
                {
                    "osm_id": highways.osm_id[index],
                    "highway": highways.tag_value(index, "highway"),
                    "name": highways.tag_value(index, "name"),
                    "status": STATUS_BY_CODE[int(road_codes[index])],
                    "confidence": CONFIDENCE_BY_CODE[int(road_tiers[index])],
                    "flooded_fraction": round(float(fractions[index]), 4),
                    "flood_probability": None
                    if not np.isfinite(means[index])
                    else round(float(means[index]), 4),
                    "max_probability": None
                    if not np.isfinite(maxima[index])
                    else round(float(maxima[index]), 4),
                    "length_km": round(length_km, 6),
                    "flooded_length_km": round(float(fractions[index]) * length_km, 6),
                    "clipped": highways.clipped[index],
                }
            )
        road_geojson = _collection(
            [
                _feature(record, highways.geometries[index], _ROAD_PROPERTIES)
                for index, record in enumerate(road_records)
                if record["status"] != "unaffected"
            ]
        )
    else:
        road_geojson = _collection([])
    timings["roads_s"] = round(time.time() - mark, 3)

    # --- bridges -----------------------------------------------------------
    mark = time.time()
    bridge_records: List[Dict[str, Any]] = []
    bridges = layers.get("bridge")
    if bridges is not None and len(bridges):
        utm_lines = project_linestrings(bridges.geometries, to_utm)
        buffered = project_geometries(
            [geom.buffer(float(cfg.damage.bridge_buffer_m)) for geom in utm_lines], to_wgs
        )
        bridge_stats = footprint_flood_stats(buffered, probability, transform, flooded_threshold)
        for index in range(len(bridges)):
            fraction = float(bridge_stats.flooded_fraction[index])
            bridge_records.append(
                {
                    "osm_id": bridges.osm_id[index],
                    "name": bridges.tag_value(index, "name"),
                    "highway": bridges.tag_value(index, "highway"),
                    "structure": bridge_structure(bridges.tags[index]),
                    "status": classify_bridge_status(fraction, possibly_fraction, config=cfg),
                    "confidence": confidence_tier(
                        fraction,
                        possibly_fraction=possibly_fraction,
                        sampled_centroid=bool(bridge_stats.sampled_centroid[index]),
                        config=cfg,
                    ),
                    "flooded_fraction": round(fraction, 4),
                    "mean_probability": round(float(bridge_stats.mean_probability[index]), 4),
                    "buffer_m": float(cfg.damage.bridge_buffer_m),
                    "length_km": round(float(utm_lines[index].length) / 1000.0, 6),
                    "clipped": bridges.clipped[index],
                }
            )
        bridge_geojson = _collection(
            [
                _feature(record, bridges.geometries[index], _BRIDGE_PROPERTIES)
                for index, record in enumerate(bridge_records)
            ]
        )
    else:
        bridge_geojson = _collection([])
    timings["bridges_s"] = round(time.time() - mark, 3)

    counts = damage_counts(codes, road_records, bridge_records)
    timings["total_s"] = round(time.time() - started, 3)

    return DamageResult(
        buildings=building_geojson,
        roads=road_geojson,
        bridges=bridge_geojson,
        counts=counts,
        facts=damage_facts(counts),
        timings=timings,
        utm_epsg=utm_epsg,
        building_records=building_records,
        building_status_codes=codes,
        building_confidence_codes=tiers,
        building_flooded_fraction=stats.flooded_fraction.astype("float32"),
        building_osm_ids=np.asarray(buildings.osm_id, dtype="int64"),
        building_points=np.column_stack([stats.points_lon, stats.points_lat]),
        road_records=road_records,
        bridge_records=bridge_records,
        limitations=limitations,
        provenance={
            "layers": {name: layer.path for name, layer in layers.items()},
            "layer_rows": {name: len(layer) for name, layer in layers.items()},
            "dropped_geometry_rows": sum(layer.dropped_geometries for layer in layers.values()),
            "bbox": bbox,
            "flooded_probability_threshold": flooded_threshold,
            "affected_overlap_fraction": affected_fraction,
            "possibly_affected_overlap_fraction": possibly_fraction,
            "confidence_high_margin": high_margin,
            "confidence_medium_margin": medium_margin,
            "bridge_buffer_m": float(cfg.damage.bridge_buffer_m),
            "road_sample_spacing_m": float(cfg.damage.road_sample_spacing_m),
            "building_sampling": {
                "footprint": int((~stats.sampled_centroid).sum()),
                "centroid": int(stats.sampled_centroid.sum()),
                "rasterise_all_touched": False,
            },
            "layer_geometry_note": (
                "the GeoJSON layers carry only affected/possibly features; unaffected "
                "features are counted in counts and reported in facts.json"
            ),
        },
    )


_BUILDING_PROPERTIES = ("sampled", "pixels", "mean_probability")
_ROAD_PROPERTIES = ("highway", "name", "length_km", "flooded_length_km", "flood_probability")
_BRIDGE_PROPERTIES = (
    "name",
    "highway",
    "structure",
    "buffer_m",
    "mean_probability",
    "length_km",
)


def damage_counts(
    building_codes: np.ndarray,
    road_records: Sequence[Dict[str, Any]],
    bridge_records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Per-type counts computed by code (the numbers that reach facts.json)."""
    codes = np.asarray(building_codes, dtype="int8")
    buildings_affected = int((codes == 2).sum())
    buildings_possibly = int((codes == 1).sum())
    roads_affected = sum(1 for r in road_records if r["status"] == "affected")
    roads_possibly = sum(1 for r in road_records if r["status"] == "possibly_affected")
    bridges_possibly = sum(1 for r in bridge_records if r["status"] == "possibly_impacted")
    return {
        "buildings_total": int(codes.shape[0]),
        "buildings_affected": buildings_affected,
        "buildings_possibly_affected": buildings_possibly,
        "buildings_unaffected": int(codes.shape[0]) - buildings_affected - buildings_possibly,
        "roads_total": len(road_records),
        "roads_affected": roads_affected,
        "roads_possibly_affected": roads_possibly,
        "road_km_total": round(sum(r["length_km"] for r in road_records), 4),
        "road_km_affected": round(
            sum(r["length_km"] for r in road_records if r["status"] == "affected"), 4
        ),
        "road_km_flooded": round(sum(r["flooded_length_km"] for r in road_records), 4),
        "bridges_total": len(bridge_records),
        "bridges_possibly_impacted": bridges_possibly,
        "bridges_unaffected": len(bridge_records) - bridges_possibly,
        "bridges_road_total": sum(1 for r in bridge_records if r["structure"] == "road_bridge"),
        "bridges_non_road_total": sum(1 for r in bridge_records if r["structure"] == "other_bridge"),
    }


def damage_facts(counts: Dict[str, Any]) -> List[Any]:
    """``FactItem`` list for facts.json (Stage 8): counts only, computed here."""
    from contracts.schemas import FactItem, PipelineStage

    def item(fact_id: str, value: Any, unit: str) -> Any:
        return FactItem(id=fact_id, value=value, unit=unit, source_stage=PipelineStage.DAMAGE)

    return [
        item("buildings_total", counts["buildings_total"], "count"),
        item("buildings_affected", counts["buildings_affected"], "count"),
        item("buildings_possibly_affected", counts["buildings_possibly_affected"], "count"),
        item("roads_affected", counts["roads_affected"], "count"),
        item("road_km_affected", counts["road_km_affected"], "km"),
        item("bridges_possibly_impacted", counts["bridges_possibly_impacted"], "count"),
    ]


def counts_report(result: DamageResult) -> str:
    """One-line count summary used by the spike row and the report layer."""
    order = (
        "buildings_total",
        "buildings_affected",
        "buildings_possibly_affected",
        "roads_total",
        "roads_affected",
        "road_km_affected",
        "bridges_total",
        "bridges_possibly_impacted",
    )
    return " ".join(f"{key}={result.counts.get(key)}" for key in order)


# ---------------------------------------------------------------------------
# GeoJSON layers / artifacts
# ---------------------------------------------------------------------------
def _feature(record: Dict[str, Any], geometry, extra_keys: Sequence[str]) -> Dict[str, Any]:
    from shapely.geometry import mapping

    properties = {
        "osm_id": record["osm_id"],
        "status": record["status"],
        "confidence": record["confidence"],
        "flooded_fraction": record["flooded_fraction"],
    }
    for key in extra_keys:
        if key in record:
            properties[key] = record[key]
    return {"type": "Feature", "properties": properties, "geometry": mapping(geometry)}


def _collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        "features": features,
    }


def write_damage_artifacts(result: DamageResult, out_dir: Path, stem: str = "damage") -> Dict[str, str]:
    """Write the three classified layers plus the counts/timing record."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, str] = {}
    for name, layer in (
        ("buildings", result.buildings),
        ("roads", result.roads),
        ("bridges", result.bridges),
    ):
        path = out_dir / f"{stem}_{name}.geojson"
        path.write_text(json.dumps(layer), encoding="utf-8")
        written[name] = str(path)
    summary_path = out_dir / f"{stem}_counts.json"
    summary_path.write_text(
        json.dumps(
            {
                "counts": result.counts,
                "timings": result.timings,
                "utm_epsg": result.utm_epsg,
                "limitations": result.limitations,
                "provenance": result.provenance,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
    written["counts"] = str(summary_path)
    return written


# ---------------------------------------------------------------------------
# Count-only shortcut for callers that already hold decoded geometries
# ---------------------------------------------------------------------------
def classify_damage_counts(
    flood_prob: Any,
    osm_layers: Dict[str, Any],
    affected_overlap: float = 0.25,
    possibly_affected_overlap: float = 0.05,
) -> Dict[str, Any]:
    """Count-only variant of :func:`classify_damage`.

    ``osm_layers`` maps a feature type to ``{"transform": ..., "geometries": [...]}``
    (WGS84 geometries). The counts equal what :func:`classify_damage` produces for
    the same inputs, without building the GeoJSON layers.
    """
    transform = (osm_layers or {}).get("transform")
    if transform is None:
        raise ValueError("osm_layers['transform'] is required to sample the raster")
    probability = np.asarray(flood_prob, dtype="float64")
    building_codes = np.zeros(0, dtype="int8")
    roads: List[Dict[str, Any]] = []
    bridges: List[Dict[str, Any]] = []
    for feature_type, payload in osm_layers.items():
        if feature_type == "transform":
            continue
        geometries = np.asarray(payload.get("geometries") or [], dtype=object)
        stats = footprint_flood_stats(geometries, probability, transform, 0.5)
        codes = status_codes(stats.flooded_fraction, affected_overlap, possibly_affected_overlap)
        if feature_type == "building":
            building_codes = codes
        elif feature_type == "highway":
            roads = [
                {"status": STATUS_BY_CODE[int(code)], "length_km": 0.0, "flooded_length_km": 0.0}
                for code in codes
            ]
        elif feature_type == "bridge":
            bridges = [
                {
                    "status": "possibly_impacted" if code else "unaffected",
                    "structure": "road_bridge",
                }
                for code in codes
            ]
    counts = damage_counts(building_codes, roads, bridges)
    return {
        "buildings_affected": counts["buildings_affected"],
        "buildings_possibly_affected": counts["buildings_possibly_affected"],
        "road_km_affected": counts["road_km_affected"],
        "bridges_possibly_impacted": counts["bridges_possibly_impacted"],
    }
