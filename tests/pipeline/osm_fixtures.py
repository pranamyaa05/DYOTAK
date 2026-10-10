"""Helpers that write ohsome v2 style GeoParquet extracts for tests.

The schema written here is the one measured on the real cached ohsome v2 files
(``osm_type``/``osm_id``/``tags`` map/``bbox`` struct/``geom_type``/``geom`` WKB/
``clipped`` bool plus the timestamp and user columns the pipeline deliberately
does not read). Writing the *whole* schema is the point: it proves the readers
prune columns and never need zoneinfo for the tz-aware timestamps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

#: Field order of the real extract.
_COLUMNS = [
    ("osm_type", pa.string()),
    ("osm_id", pa.int64()),
    ("version", pa.int32()),
    ("minor_version", pa.int32()),
    ("edits", pa.int32()),
    ("user_id", pa.int32()),
    ("user_name", pa.string()),
    ("changeset_id", pa.int64()),
    ("tags", pa.map_(pa.string(), pa.string())),
    (
        "bbox",
        pa.struct(
            [
                ("xmin", pa.float64()),
                ("xmax", pa.float64()),
                ("ymin", pa.float64()),
                ("ymax", pa.float64()),
            ]
        ),
    ),
    ("geom_type", pa.string()),
    ("geom", pa.binary()),
    ("clipped", pa.bool_()),
]

#: Columns the real files carry that this helper leaves null (never read).
_EXTRA_COLUMNS = ("version", "minor_version", "edits", "user_id", "user_name", "changeset_id")


def feature(
    osm_id: int,
    geometry,
    tags: Optional[Dict[str, str]] = None,
    osm_type: str = "way",
    clipped: bool = False,
) -> Dict[str, Any]:
    """One extract row for a shapely geometry."""
    return {
        "osm_type": osm_type,
        "osm_id": int(osm_id),
        "tags": dict(tags or {}),
        "geometry": geometry,
        "clipped": clipped,
    }


def write_geoparquet(path: Path, features: Sequence[Dict[str, Any]]) -> Path:
    """Write extract rows to ``path`` as GeoParquet with the real schema."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = [shapely.bounds(row["geometry"]) for row in features]
    arrays: Dict[str, pa.Array] = {}
    for name, kind in _COLUMNS:
        if name in _EXTRA_COLUMNS:
            arrays[name] = pa.array([None] * len(features), type=kind)
        elif name == "tags":
            arrays[name] = pa.array([row["tags"] for row in features], type=kind)
        elif name == "bbox":
            arrays[name] = pa.array(
                [
                    {"xmin": b[0], "xmax": b[2], "ymin": b[1], "ymax": b[3]}
                    for b in bounds
                ],
                type=kind,
            )
        elif name == "geom":
            arrays[name] = pa.array(
                [shapely.to_wkb(row["geometry"]) for row in features], type=kind
            )
        elif name == "geom_type":
            arrays[name] = pa.array(
                [str(row["geometry"].geom_type) for row in features], type=kind
            )
        elif name == "clipped":
            arrays[name] = pa.array([bool(row["clipped"]) for row in features], type=kind)
        else:
            arrays[name] = pa.array([row[name] for row in features], type=kind)
    table = pa.table(arrays)
    pq.write_table(table, path)
    return path


def write_extract_map(
    directory: Path, layers: Dict[str, Sequence[Dict[str, Any]]]
) -> Dict[str, str]:
    """Write one GeoParquet file per feature type, keyed like ``parquet_paths``."""
    paths: Dict[str, str] = {}
    for feature_type, features in layers.items():
        target = Path(directory) / f"{feature_type}.parquet"
        write_geoparquet(target, features)
        paths[feature_type] = str(target)
    return paths


def square_box(cx: float, cy: float, size: float):
    """Axis-aligned lon/lat square of side ``size`` (degrees) around a centre."""
    half = size / 2.0
    return shapely.box(cx - half, cy - half, cx + half, cy + half)


def line(points: Iterable[Tuple[float, float]]):
    """LineString from ``(lon, lat)`` pairs."""
    return shapely.linestrings([(x, y) for x, y in points])


class FloodStub:
    """Minimal stand-in for ``flood_baseline.FloodMapResult`` used by stages 6/7.

    Carries exactly the attributes those stages read: the probability raster, the
    grid transform and the ``bbox`` provenance entry.
    """

    def __init__(self, probability, transform, bbox: Sequence[float]) -> None:
        self.probability = probability
        self.mask = probability >= 0.5
        self.transform = transform
        self.provenance = {"bbox": [float(c) for c in bbox]}
        self.utm_epsg = 0
        self.counts = {"final_mask_pixels": int(self.mask.sum())}
        self.area_km2 = 0.0
        self.crs = "EPSG:4326"


def flood_stub(
    patch_rows: Sequence[int] = (2, 3, 4),
    patch_cols: Sequence[int] = (2, 3, 4),
    shape: Tuple[int, int] = (10, 10),
    bbox: Sequence[float] = (85.0, 27.0, 85.1, 27.1),
) -> Tuple[FloodStub, Any]:
    """A 10x10 test grid with a fully flooded patch, plus its transform.

    Pixel ``(row, col)`` covers ``x = bbox[0] + col * 0.01`` and
    ``y = bbox[3] - row * 0.01``; its centre is half a pixel further in.
    """
    from app.pipeline.flood_baseline import grid_transform

    probability = np.zeros(shape, dtype="float32")
    rows = list(patch_rows)
    cols = list(patch_cols)
    probability[np.ix_(rows, cols)] = 1.0
    transform = grid_transform(list(bbox), shape)
    return FloodStub(probability, transform, list(bbox)), transform


def pixel_centre(
    col: int, row: int, bbox: Sequence[float] = (85.0, 27.0, 85.1, 27.1)
) -> Tuple[float, float]:
    """Centre (lon, lat) of pixel ``(row, col)`` on the 10x10 test grid."""
    return bbox[0] + (col + 0.5) * 0.01, bbox[3] - (row + 0.5) * 0.01


#: Keep the pixel centres strictly inside the polygon: a centre exactly on an
#: edge is a boundary case with no meaningful answer.
PIXEL_PAD = 1e-4


def pixel_box(
    col_lo: int,
    row_lo: int,
    col_hi: int,
    row_hi: int,
    bbox: Sequence[float] = (85.0, 27.0, 85.1, 27.1),
):
    """Square that covers exactly the pixel centres ``col_lo..col_hi`` x ``row_lo..row_hi``.

    ``pixel_box(3, 3, 4, 4)`` therefore covers 4 pixel centres, which is what the
    footprint assertions count.
    """
    x_lo = bbox[0] + col_lo * 0.01 + PIXEL_PAD
    x_hi = bbox[0] + (col_hi + 1) * 0.01 - PIXEL_PAD
    y_hi = bbox[3] - row_lo * 0.01 - PIXEL_PAD
    y_lo = bbox[3] - (row_hi + 1) * 0.01 + PIXEL_PAD
    return shapely.box(x_lo, y_lo, x_hi, y_hi)


def pixel_span(
    indices: Sequence[int], bbox: Sequence[float] = (85.0, 27.0, 85.1, 27.1)
) -> Tuple[float, float]:
    """(min, max) coordinate covered by the given pixel indices along one axis.

    Used for horizontal/vertical test roads: the span runs from the outer edge of
    the first pixel to the outer edge of the last one, so every pixel centre in
    between is sampled.
    """
    return bbox[0] + min(indices) * 0.01 + PIXEL_PAD, bbox[0] + (max(indices) + 1) * 0.01 - PIXEL_PAD
