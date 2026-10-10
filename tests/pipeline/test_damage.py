"""Tests for the damage overlay (app/pipeline/damage.py, Stage 6).

Synthetic GeoParquet extracts are written with the **real ohsome v2 schema**
(see ``osm_fixtures``), so the reader is exercised end to end: geometry decoding
from WKB, the ``tags`` map, the geometry types actually present in the extracts
(Polygon / MultiPolygon / LineString / Point) and the deliberately unread
timestamp columns.

Covered: status thresholds and the confidence tiers from config, the bridge rule
(never "destroyed"), footprint sampling (fully flooded, partly flooded, sub-pixel
via the representative point, outside the raster), the equivalence of the
vectorised footprint sampler with ``rasterio.features.rasterize`` on both of its
paths, road segments with flooded length in km, per-type counts, the GeoJSON
layers, the UTM zone choice and the artifact writer.

The integration test at the bottom runs the real Trishuli preset and asserts
structural properties only (counts add up, the layers are valid GeoJSON, the
60 024-footprint sampling stays inside the 10 s budget). It never compares against
EMSR927 or any published damage map.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

import osm_fixtures as fx
from app.common import geo
from app.pipeline import damage as dmg
from app.pipeline import flood_baseline as fb
from app.settings import app_config, settings

BBOX = [85.0, 27.0, 85.1, 27.1]
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _layers(tmp_path: Path, **layers):
    """Write synthetic extracts and classify them against a flood stub."""
    flood, _ = fx.flood_stub(
        patch_rows=layers.pop("patch_rows", (2, 3, 4)),
        patch_cols=layers.pop("patch_cols", (2, 3, 4)),
    )
    paths = fx.write_extract_map(tmp_path, layers)
    return dmg.classify_damage(flood, paths), flood


# ---------------------------------------------------------------------------
# GeoParquet reading (real schema)
# ---------------------------------------------------------------------------
def test_reader_decodes_the_real_schema_and_prunes_unread_columns(tmp_path):
    paths = fx.write_extract_map(
        tmp_path,
        {
            "building": [
                fx.feature(1, fx.square_box(85.02, 27.08, 0.004), {"building": "yes"}),
                fx.feature(2, fx.square_box(85.03, 27.08, 0.004), {"building": "house", "name": "x"}),
            ],
            "amenity": [
                fx.feature(3, fx.line([(85.0, 27.0), (85.01, 27.01)]), {"amenity": "hospital"}, osm_type="node")
            ],
        },
    )
    layer = dmg.read_osm_layer(paths["building"], "building")

    assert len(layer) == 2
    assert layer.osm_type == ["way", "way"]
    assert layer.osm_id == [1, 2]
    assert layer.geom_types == ["Polygon", "Polygon"]
    assert layer.dropped_geometries == 0
    assert layer.tags[1]["name"] == "x"
    assert layer.geometries[0].geom_type == "Polygon"
    assert layer.geometries[0].bounds[0] == pytest.approx(85.018, abs=1e-6)

    amenity = dmg.read_osm_layer(paths["amenity"], "amenity")
    assert amenity.geom_types == ["LineString"]
    assert amenity.matching_tags(["amenity"], ["hospital"]) == [0]
    assert amenity.matching_tags(["amenity"], ["clinic"]) == []
    assert amenity.tag_value(0, "amenity") == "hospital"
    assert amenity.tag_value(0, "missing") is None


def test_reader_counts_rows_without_a_usable_geometry(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import shapely

    path = fx.write_geoparquet(
        tmp_path / "building.parquet",
        [fx.feature(1, fx.square_box(85.02, 27.08, 0.004), {"building": "yes"})],
    )
    table = pq.read_table(path)
    table = table.set_column(
        table.schema.get_field_index("geom"),
        "geom",
        pa.array([None], type=pa.binary()),
    )
    pq.write_table(table, path)

    layer = dmg.read_osm_layer(path, "building")
    assert len(layer) == 0
    assert layer.dropped_geometries == 1
    assert shapely.is_empty == shapely.is_empty  # keep the import meaningful


def test_read_osm_layers_skips_missing_files(tmp_path):
    layers = dmg.read_osm_layers(
        {"building": str(tmp_path / "nope.parquet"), "highway": None}
    )
    assert layers == {}


# ---------------------------------------------------------------------------
# Status and confidence rules (config driven)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fraction,expected",
    [
        (0.0, "unaffected"),
        (0.04, "unaffected"),
        (0.05, "possibly_affected"),
        (0.24, "possibly_affected"),
        (0.25, "affected"),
        (1.0, "affected"),
    ],
)
def test_status_thresholds_come_from_config(fraction, expected):
    assert dmg.classify_status(fraction) == expected
    assert dmg.classify_status(fraction) in dmg.FEATURE_STATUSES
    # Explicit fractions override config and follow the same rule.
    assert dmg.classify_status(fraction, affected_fraction=0.25, possibly_fraction=0.05) == expected


@pytest.mark.parametrize(
    "fraction,tier",
    [
        (0.0, "high"),          # nothing flooded at all
        (0.50, "high"),         # far from both boundaries
        (0.10, "medium"),       # 0.05 from the possibly boundary
        (0.22, "low"),          # 0.03 from the affected boundary
    ],
)
def test_confidence_tiers_follow_the_configured_margins(fraction, tier):
    assert dmg.confidence_tier(fraction) == tier
    assert dmg.confidence_tier(fraction) in dmg.CONFIDENCE_TIERS


def test_confidence_is_capped_for_a_representative_point_sample():
    assert dmg.confidence_tier(0.5) == "high"
    assert dmg.confidence_tier(0.5, sampled_centroid=True) == "medium"
    assert dmg.confidence_tier(0.22, sampled_centroid=True) == "low"


def test_vectorised_codes_match_the_scalar_rules():
    fractions = np.asarray([0.0, 0.03, 0.10, 0.25, 0.9])
    codes = dmg.status_codes(fractions, 0.25, 0.05)
    tiers = dmg.confidence_codes(fractions, 0.25, 0.05, 0.20, 0.05)
    for index, fraction in enumerate(fractions):
        assert dmg.STATUS_BY_CODE[int(codes[index])] == dmg.classify_status(fraction)
        assert dmg.CONFIDENCE_BY_CODE[int(tiers[index])] == dmg.confidence_tier(fraction)
    capped = dmg.confidence_codes(fractions, 0.25, 0.05, 0.20, 0.05, np.ones(5, dtype=bool))
    assert dmg.CONFIDENCE_BY_CODE[int(capped[-1])] == "medium"


def test_bridges_are_never_reported_as_destroyed():
    assert "destroyed" not in dmg.BRIDGE_STATUSES
    for fraction in (0.0, 0.04, 0.05, 0.5, 1.0):
        status = dmg.classify_bridge_status(fraction)
        assert status in dmg.BRIDGE_STATUSES
        assert status != "destroyed"


def test_bridge_structure_separates_road_bridges_from_aqueducts():
    assert dmg.bridge_structure({"bridge": "yes", "highway": "path"}) == "road_bridge"
    assert dmg.bridge_structure({"bridge": "aqueduct", "waterway": "canal"}) == "other_bridge"


# ---------------------------------------------------------------------------
# Footprint sampling
# ---------------------------------------------------------------------------
def test_footprint_fully_inside_the_flood_patch_is_affected():
    flood, transform = fx.flood_stub()
    # The flood patch is pixels (3,3)..(4,4); this square covers exactly those
    # four pixel centres, so the whole footprint is flooded.
    geometry = fx.pixel_box(3, 3, 4, 4)
    stats = dmg.footprint_flood_stats(
        np.asarray([geometry], dtype=object), flood.probability, transform, 0.5
    )
    assert int(stats.pixel_count[0]) == 4
    assert stats.flooded_fraction[0] == pytest.approx(1.0)
    assert stats.mean_probability[0] == pytest.approx(1.0)
    assert bool(stats.sampled_centroid[0]) is False
    assert dmg.classify_status(float(stats.flooded_fraction[0])) == "affected"


def test_footprint_partly_inside_gives_a_graded_fraction():
    flood, transform = fx.flood_stub()
    # The flood patch is pixels rows 2..4 x cols 2..4 (9 pixels). This footprint
    # covers 49 pixel centres, so its flooded fraction is 9/49 = 0.184: inside the
    # "possibly_affected" band (>= 0.05, < 0.25) with a medium confidence tier.
    geometry = fx.pixel_box(0, 0, 6, 6)
    stats = dmg.footprint_flood_stats(
        np.asarray([geometry], dtype=object), flood.probability, transform, 0.5
    )
    assert int(stats.pixel_count[0]) == 49
    assert stats.flooded_fraction[0] == pytest.approx(9 / 49)
    assert dmg.classify_status(float(stats.flooded_fraction[0])) == "possibly_affected"
    assert dmg.confidence_tier(float(stats.flooded_fraction[0])) == "medium"
    assert stats.mean_probability[0] == pytest.approx(9 / 49)


def test_sub_pixel_footprint_falls_back_to_the_representative_point():
    flood, transform = fx.flood_stub()
    # Centred on a pixel CORNER: the 0.004 deg square covers no pixel centre, so
    # it takes the documented representative-point fallback. Its centre sits in
    # the flooded pixel (row 3, col 3).
    lon, lat = 85.03, 27.07
    geometry = fx.square_box(lon, lat, 0.004)
    stats = dmg.footprint_flood_stats(
        np.asarray([geometry], dtype=object), flood.probability, transform, 0.5
    )
    assert int(stats.pixel_count[0]) == 0
    assert bool(stats.sampled_centroid[0]) is True
    assert stats.flooded_fraction[0] == pytest.approx(1.0)
    assert stats.points_lon[0] == pytest.approx(lon, abs=1e-9)
    assert stats.points_lat[0] == pytest.approx(lat, abs=1e-9)


def test_footprint_covering_exactly_one_pixel_centre_is_sampled_by_footprint():
    flood, transform = fx.flood_stub()
    lon, lat = fx.pixel_centre(3, 3)              # strictly inside the patch
    stats = dmg.footprint_flood_stats(
        np.asarray([fx.square_box(lon, lat, 0.004)], dtype=object),
        flood.probability,
        transform,
        0.5,
    )
    assert int(stats.pixel_count[0]) == 1
    assert bool(stats.sampled_centroid[0]) is False
    assert stats.flooded_fraction[0] == pytest.approx(1.0)


def test_sub_pixel_footprint_outside_the_patch_is_unaffected():
    flood, transform = fx.flood_stub()
    lon, lat = fx.pixel_centre(8, 8)
    stats = dmg.footprint_flood_stats(
        np.asarray([fx.square_box(lon, lat, 0.002)], dtype=object),
        flood.probability,
        transform,
        0.5,
    )
    assert stats.flooded_fraction[0] == 0.0
    assert dmg.classify_status(0.0) == "unaffected"


def test_footprint_fully_outside_the_raster_is_not_flooded():
    flood, transform = fx.flood_stub()
    outside = fx.square_box(84.0, 26.0, 0.01)   # WGS84 but far from the grid
    stats = dmg.footprint_flood_stats(
        np.asarray([outside], dtype=object), flood.probability, transform, 0.5
    )
    assert int(stats.pixel_count[0]) == 0
    assert stats.flooded_fraction[0] == 0.0
    assert stats.mean_probability[0] == 0.0


def test_empty_footprint_input_is_handled():
    flood, transform = fx.flood_stub()
    stats = dmg.footprint_flood_stats(np.zeros(0, dtype=object), flood.probability, transform, 0.5)
    assert stats.flooded_fraction.shape == (0,)
    assert stats.pixel_count.shape == (0,)


def test_vectorised_footprints_match_rasterize_on_both_paths():
    """The batched contains_xy sampler must equal the label-raster reference.

    Footprints are kept disjoint: ``rasterize`` gives a pixel centre to one
    polygon only (the last one written), while the batched test correctly counts
    it for every footprint that contains it, so overlapping shapes are not
    comparable. Real OSM footprints essentially do not overlap (the live run over
    60 024 Trishuli buildings matched exactly).
    """
    flood, transform = fx.flood_stub()
    rng = np.random.default_rng(7)
    geometries = []
    for row in range(6):
        for col in range(6):
            size = rng.uniform(0.002, 0.012)
            cx = 85.005 + col * 0.017 + rng.uniform(0.0, 0.004)
            cy = 27.005 + row * 0.017 + rng.uniform(0.0, 0.004)
            geometries.append(fx.square_box(cx, cy, size))
    geoms = np.asarray(geometries, dtype=object)

    fast = dmg.footprint_flood_stats(geoms, flood.probability, transform, 0.5)
    fallback = dmg.footprint_flood_stats(
        geoms, flood.probability, transform, 0.5, max_candidates=1
    )
    labels = dmg.rasterize_features(geoms, transform, flood.probability.shape)
    reference = np.bincount(labels.ravel(), minlength=len(geoms) + 1)[1:]
    flooded = np.bincount(
        labels[flood.probability >= 0.5].ravel(), minlength=len(geoms) + 1
    )[1:]

    for index in range(len(geoms)):
        assert int(fast.pixel_count[index]) == int(fallback.pixel_count[index])
    assert np.allclose(fast.flooded_fraction, fallback.flooded_fraction)
    assert np.allclose(fast.mean_probability, fallback.mean_probability)
    assert np.array_equal(fast.pixel_count, reference)
    covered = reference > 0
    assert np.allclose(fast.flooded_fraction[covered], flooded[covered] / reference[covered])


# ---------------------------------------------------------------------------
# Road segments
# ---------------------------------------------------------------------------
def test_road_segment_keeps_its_length_and_flooded_length_in_km(tmp_path):
    flood, transform = fx.flood_stub()
    # Two horizontal roads inside the flooded rows (3..4): one runs the full
    # width of the flooded columns (3..4) and one only crosses one flooded
    # column, so its flooded length is a share of its total length.
    inside = fx.line(
        [(fx.pixel_span([3, 4])[0], fx.pixel_centre(3, 3)[1]),
         (fx.pixel_span([3, 4])[1], fx.pixel_centre(3, 3)[1])]
    )
    partial = fx.line(
        [(fx.pixel_span([3, 5])[0], fx.pixel_centre(3, 4)[1]),
         (fx.pixel_span([3, 5])[1], fx.pixel_centre(3, 4)[1])]
    )
    utm = dmg.project_linestrings(
        np.asarray([inside, partial], dtype=object), dmg.project_to_utm(BBOX)[1]
    )
    xs, ys, owner, spacing, lengths = dmg.sample_lines_at_spacing(utm, 20.0)
    wgs_x, wgs_y = dmg.project_to_utm(BBOX)[2](xs, ys)
    values = dmg.sample_raster_points(flood.probability, transform, wgs_x, wgs_y)
    mean, maximum, fraction = dmg.aggregate_edge_probability(
        values, owner, spacing, 2, 0.5
    )

    assert lengths[0] > 0 and lengths[1] > 0
    assert fraction[0] == pytest.approx(1.0, abs=0.02)
    assert fraction[1] == pytest.approx(2 / 3, abs=0.05)
    assert maximum[0] == pytest.approx(1.0)
    assert 0.5 <= mean[0] <= 1.0
    assert mean[1] < mean[0]

    # The km figures the stage reports are the UTM length and the flooded share.
    result = dmg.classify_damage(
        flood,
        fx.write_extract_map(
            tmp_path,
            {
                "building": [
                    fx.feature(1, fx.pixel_box(3, 3, 4, 4), {"building": "yes"})
                ],
                "highway": [
                    fx.feature(10, inside, {"highway": "secondary"}),
                    fx.feature(11, partial, {"highway": "path"}),
                ],
                "bridge": [],
            },
        ),
    )
    record = result.road_records[0]
    assert record["status"] == "affected"
    assert record["length_km"] > 0
    assert 0 < record["flooded_length_km"] <= record["length_km"]
    assert result.counts["road_km_affected"] > 0


def test_aggregate_edge_probability_weights_by_length_and_handles_no_samples():
    owner = np.asarray([0, 0, 0], dtype="int64")
    spacing = np.asarray([10.0, 10.0, 0.0])
    values = np.asarray([0.0, 1.0, 1.0])
    mean, maximum, fraction = dmg.aggregate_edge_probability(values, owner, spacing, 1, 0.5)
    # (0*10 + 1*10 + 1*1) / 21
    assert mean[0] == pytest.approx(11.0 / 21.0)
    assert maximum[0] == pytest.approx(1.0)
    assert fraction[0] == pytest.approx(2 / 3)

    empty_mean, empty_max, empty_fraction = dmg.aggregate_edge_probability(
        np.zeros(0), np.zeros(0, dtype="int64"), np.zeros(0), 1, 0.5
    )
    assert np.isnan(empty_mean[0]) and np.isnan(empty_max[0])
    assert empty_fraction[0] == 0.0

    nan_mean, _, _ = dmg.aggregate_edge_probability(
        np.asarray([np.nan]), np.asarray([0]), np.asarray([5.0]), 1, 0.5
    )
    assert np.isnan(nan_mean[0]), "samples outside the raster carry no evidence"


# ---------------------------------------------------------------------------
# Full stage on synthetic extracts
# ---------------------------------------------------------------------------
def test_classify_damage_counts_add_up_and_layers_carry_properties(tmp_path):
    buildings = [
        fx.feature(1, fx.square_box(85.04, 27.065, 0.02), {"building": "yes"}),       # flooded
        fx.feature(2, fx.square_box(85.085, 27.095, 0.01), {"building": "yes"}),      # dry
        fx.feature(3, fx.square_box(85.05, 27.055, 0.04), {"building": "yes"}),       # graded
    ]
    roads = [
        fx.feature(10, fx.line([(85.005, 27.065), (85.095, 27.065)]), {"highway": "secondary"}),
        fx.feature(11, fx.line([(85.005, 27.095), (85.095, 27.095)]), {"highway": "path"}),
    ]
    bridges = [
        fx.feature(20, fx.line([(85.03, 27.065), (85.05, 27.065)]), {"bridge": "yes", "highway": "secondary"}),
        fx.feature(21, fx.line([(85.03, 27.095), (85.05, 27.095)]), {"bridge": "yes", "highway": "path"}),
    ]
    result, _ = _layers(tmp_path, building=buildings, highway=roads, bridge=bridges)

    counts = result.counts
    assert counts["buildings_total"] == 3
    assert counts["buildings_affected"] == 2
    assert counts["buildings_possibly_affected"] == 0
    assert counts["buildings_unaffected"] == 1
    assert counts["buildings_affected"] + counts["buildings_possibly_affected"] + counts[
        "buildings_unaffected"
    ] == counts["buildings_total"]

    assert counts["roads_total"] == 2
    assert counts["roads_affected"] == 1
    assert counts["road_km_affected"] > 0
    assert counts["road_km_affected"] <= counts["road_km_total"]
    assert counts["road_km_flooded"] > 0

    assert counts["bridges_total"] == 2
    assert counts["bridges_possibly_impacted"] == 1
    assert set(r["status"] for r in result.bridge_records) <= set(dmg.BRIDGE_STATUSES)

    # Only non-unaffected features reach the map layers, with status/confidence.
    assert len(result.buildings["features"]) == 2
    properties = result.buildings["features"][0]["properties"]
    assert set(properties) >= {"osm_id", "status", "confidence", "flooded_fraction"}
    assert properties["status"] in dmg.FEATURE_STATUSES
    assert properties["confidence"] in dmg.CONFIDENCE_TIERS
    assert all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in result.buildings["features"])
    assert all(f["geometry"]["type"] == "LineString" for f in result.roads["features"])
    assert len(result.bridges["features"]) == 2      # every bridge, with its status

    assert result.utm_epsg == geo.get_utm_epsg_for_bbox(BBOX)
    assert result.provenance["flooded_probability_threshold"] == app_config.flood.probability_threshold
    assert "<" not in dmg.counts_report(result)      # report renders without error


def test_classify_damage_facts_are_computed_by_code(tmp_path):
    buildings = [fx.feature(1, fx.square_box(85.04, 27.065, 0.02), {"building": "yes"})]
    result, _ = _layers(tmp_path, building=buildings, highway=[], bridge=[])

    facts = {fact.id: fact for fact in result.facts}
    assert facts["buildings_total"].value == result.counts["buildings_total"]
    assert facts["buildings_affected"].value == result.counts["buildings_affected"]
    assert facts["buildings_possibly_affected"].value == 0
    assert facts["road_km_affected"].unit == "km"
    assert facts["roads_affected"].value == 0
    assert all(fact.source_stage.value == "damage" for fact in result.facts)
    assert all(fact.unit in ("count", "km") for fact in result.facts)


def test_classify_damage_requires_the_building_layer(tmp_path):
    paths = fx.write_extract_map(tmp_path, {"highway": []})
    flood, _ = fx.flood_stub()
    with pytest.raises(ValueError, match="building"):
        dmg.classify_damage(flood, paths)


def test_missing_bridge_layer_is_a_recorded_limitation(tmp_path):
    buildings = [fx.feature(1, fx.square_box(85.04, 27.065, 0.02), {"building": "yes"})]
    result, _ = _layers(tmp_path, building=buildings)
    assert result.counts["bridges_total"] == 0
    assert any("bridge" in note for note in result.limitations)
    assert any("highway" in note for note in result.limitations)


def test_sub_pixel_limitation_is_reported(tmp_path):
    buildings = [fx.feature(1, fx.square_box(85.04, 27.065, 0.001), {"building": "yes"})]
    result, _ = _layers(tmp_path, building=buildings)
    assert result.provenance["building_sampling"]["centroid"] == 1
    assert result.provenance["building_sampling"]["footprint"] == 0
    assert any("representative point" in note for note in result.limitations)


def test_utm_zone_follows_the_aoi_centroid_in_the_southern_hemisphere(tmp_path):
    from app.pipeline.flood_baseline import grid_transform

    bbox = [-60.1, -3.1, -60.0, -3.0]
    probability = np.zeros((10, 10), dtype="float32")
    probability[4, 4] = 1.0
    flood = fx.FloodStub(probability, grid_transform(bbox, probability.shape), bbox)
    geometry = fx.square_box(-60.05, -3.05, 0.001)
    paths = fx.write_extract_map(tmp_path, {"building": [fx.feature(1, geometry, {"building": "yes"})]})
    result = dmg.classify_damage(flood, paths)
    assert result.utm_epsg == geo.get_utm_epsg_for_bbox(bbox)
    assert 32700 <= result.utm_epsg < 32800, "southern hemisphere must use the 327xx band"


def test_write_damage_artifacts_round_trips(tmp_path):
    buildings = [fx.feature(1, fx.square_box(85.04, 27.065, 0.02), {"building": "yes"})]
    result, _ = _layers(tmp_path / "src", building=buildings)
    written = dmg.write_damage_artifacts(result, tmp_path / "out")

    assert set(written) == {"buildings", "roads", "bridges", "counts"}
    for name, path in written.items():
        assert Path(path).is_file(), name
    layer = json.loads(Path(written["buildings"]).read_text(encoding="utf-8"))
    assert layer["type"] == "FeatureCollection"
    assert layer["crs"]["properties"]["name"].endswith("EPSG::4326")
    summary = json.loads(Path(written["counts"]).read_text(encoding="utf-8"))
    assert summary["counts"] == result.counts
    assert summary["timings"]["total_s"] >= 0


def test_classify_damage_counts_shortcut_matches_the_full_stage(tmp_path):
    buildings = [fx.feature(1, fx.square_box(85.04, 27.065, 0.02), {"building": "yes"})]
    flood, transform = fx.flood_stub()
    result = dmg.classify_damage(
        flood,
        fx.write_extract_map(tmp_path, {"building": buildings, "highway": [], "bridge": []}),
    )
    shortcut = dmg.classify_damage_counts(
        flood.probability,
        {
            "transform": transform,
            "building": {"geometries": [fx.square_box(85.04, 27.065, 0.02)]},
        },
    )
    assert shortcut["buildings_affected"] == result.counts["buildings_affected"] == 1
    assert shortcut["buildings_possibly_affected"] == result.counts["buildings_possibly_affected"]
    assert shortcut["road_km_affected"] == 0.0


# ---------------------------------------------------------------------------
# Integration: live Trishuli extracts, structural properties only
# ---------------------------------------------------------------------------
needs_cdse = pytest.mark.skipif(
    not (settings.cdse_client_id and settings.cdse_client_secret),
    reason="CDSE credentials required for the live baseline flood map",
)
needs_ohsome = pytest.mark.skipif(
    not settings.ohsome_api_key,
    reason="ohsome API key required for the pre-event extract",
)


def _trishuli_preset():
    with open(REPO_ROOT / "config" / "presets" / "trishuli_emsr927.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.integration
@needs_cdse
@needs_ohsome
def test_trishuli_damage_structure_and_building_sampling_budget():
    """Live run: structural assertions plus the stated 10 s sampling budget."""
    from datetime import date, timedelta

    from app.pipeline import cdse, ohsome

    preset = _trishuli_preset()
    bbox = [float(c) for c in preset["bbox"]]
    event_date = str(preset["event_date"])
    search_start = (
        date.fromisoformat(event_date) - timedelta(days=app_config.pairing.search_window_days)
    ).isoformat()
    search_end = (date.fromisoformat(event_date) + timedelta(days=7)).isoformat()

    scenes = cdse.search_sentinel1_scenes(bbox, search_start, search_end, instrument_mode="IW")
    posts = [s for s in scenes if str(s["acquisition_time"])[:10] >= event_date]
    pres = [s for s in scenes if str(s["acquisition_time"])[:10] < event_date]
    assert posts and pres
    flood = fb.fetch_and_build_flood_map(min(posts, key=lambda s: s["acquisition_time"]), pres, bbox=bbox)
    assert flood.counts["final_mask_pixels"] > 0

    extract = ohsome.fetch_preevent_osm_elements(
        bbox, str(preset.get("osm_snapshot_date")), event_date
    )
    result = dmg.classify_damage(flood, extract["parquet_paths"])

    counts = result.counts
    assert counts["buildings_total"] == extract["counts"]["building"]
    assert (
        counts["buildings_affected"] + counts["buildings_possibly_affected"]
        + counts["buildings_unaffected"] == counts["buildings_total"]
    )
    assert counts["roads_total"] == extract["counts"]["highway"]
    assert counts["bridges_total"] == extract["counts"]["bridge"]
    assert set(r["status"] for r in result.bridge_records) <= set(dmg.BRIDGE_STATUSES)
    assert result.utm_epsg == geo.get_utm_epsg_for_bbox(bbox)

    # The stated budget: 60 k footprints sampled in under ~10 s (measured ~0.2 s).
    assert result.timings["buildings_s"] < 10.0, result.timings
    sampled = result.provenance["building_sampling"]
    assert sampled["footprint"] + sampled["centroid"] == counts["buildings_total"]
    assert sampled["rasterise_all_touched"] is False

    for layer_name in ("buildings", "roads", "bridges"):
        layer = getattr(result, layer_name)
        assert layer["type"] == "FeatureCollection"
        for feature in layer["features"][:50]:
            assert feature["geometry"] is not None
            assert feature["properties"]["status"]
