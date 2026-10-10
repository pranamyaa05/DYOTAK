"""Tests for the baseline flood map (app/pipeline/flood_baseline.py).

Every step gets a synthetic-array test: speckle filtering, the pre-event median,
the log-ratio + combined VV+VH rule, the Otsu split and its configured fallback
range, the probability ramp, each exclusion (permanent water, slope, radar
shadow, HAND), component cleaning, GeoJSON polygons, the UTM area, the empty
input and the refusal path.

The integration test at the bottom runs the real Trishuli preset (live CDSE
clips + Copernicus DEM) and asserts **structural properties only** - a non-empty
mask, an area inside the configured sanity range, valid polygons and consistent
counts. It never compares against EMSR927 or any published flood map.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from app.common import geo
from app.common.errors import NoValidOrbitPairError
from app.pipeline import flood_baseline as fb
from app.pipeline import terrain
from app.pipeline import water_mask
from app.settings import app_config, settings

TRISHULI_BBOX = [85.15, 27.85, 85.35, 28.05]
EVENT_DATE = "2026-08-26"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _grid_transform(shape, bbox=TRISHULI_BBOX):
    return fb.grid_transform(bbox, shape)


def _scene_pair(shape=(40, 40), patch=slice(8, 14), cols=slice(8, 16), drop_db=8.0):
    """Pre/post dB arrays with a known flood patch, plus the truth mask."""
    pre_vv = np.full(shape, -10.0, dtype="float32")
    pre_vh = np.full(shape, -16.0, dtype="float32")
    post_vv = pre_vv.copy()
    post_vh = pre_vh.copy()
    post_vv[patch, cols] -= drop_db
    post_vh[patch, cols] -= drop_db + 1.0
    truth = np.zeros(shape, dtype=bool)
    truth[patch, cols] = True
    return pre_vv, pre_vh, post_vv, post_vh, truth


# ---------------------------------------------------------------------------
# Step 1: speckle reduction
# ---------------------------------------------------------------------------
def test_speckle_filter_removes_single_pixel_spikes_and_keeps_nodata():
    band = np.full((9, 9), -12.0, dtype="float32")
    band[4, 4] = 20.0                      # isolated speckle spike
    band[0, 0] = np.nan                    # nodata must stay nodata
    filtered = fb.speckle_filter_db(band, window=3)
    assert filtered[4, 4] == pytest.approx(-12.0)
    assert np.isnan(filtered[0, 0])
    # A uniform region is untouched.
    assert filtered[7, 7] == pytest.approx(-12.0)


def test_speckle_filter_uses_the_configured_window_and_passes_through_one():
    band = np.full((5, 5), -10.0, dtype="float32")
    band[2, 2] = 5.0
    assert fb.speckle_filter_db(band, window=1)[2, 2] == pytest.approx(5.0)
    # Default window comes from config (flood.speckle_filter_size).
    default = fb.speckle_filter_db(band)
    assert default[2, 2] == pytest.approx(-10.0)
    # Even windows are rounded up to the next odd size.
    assert fb.speckle_filter_db(band, window=2)[2, 2] == pytest.approx(-10.0)
    assert app_config.flood.speckle_filter_size == 3


# ---------------------------------------------------------------------------
# Step 2: pre-event reference (median of up to N scenes)
# ---------------------------------------------------------------------------
def test_median_pre_composite_takes_the_median_and_ignores_nodata():
    a = np.full((3, 3), -10.0, dtype="float32")
    b = np.full((3, 3), -12.0, dtype="float32")
    c = np.full((3, 3), 40.0, dtype="float32")   # outlier scene
    c[0, 0] = np.nan                             # nodata in this scene only
    composite = fb.median_pre_composite([a, b, c])
    assert composite[1, 1] == pytest.approx(-10.0)   # median of 3
    assert composite[0, 0] == pytest.approx(-11.0)   # median of the two finite


def test_median_pre_composite_with_one_scene_is_that_scene():
    only = np.full((2, 2), -13.0, dtype="float32")
    assert fb.median_pre_composite([only])[0, 0] == pytest.approx(-13.0)
    with pytest.raises(ValueError):
        fb.median_pre_composite([])


# ---------------------------------------------------------------------------
# Step 3: log-ratio + combined VV+VH rule
# ---------------------------------------------------------------------------
def test_log_ratio_is_post_minus_pre_and_rejects_shape_mismatch():
    post = np.array([[0.0, -12.0]], dtype="float32")
    pre = np.array([[-5.0, -2.0]], dtype="float32")
    ratio = fb.log_ratio_db(post, pre)
    assert ratio.ravel().tolist() == pytest.approx([5.0, -10.0])
    with pytest.raises(ValueError):
        fb.log_ratio_db(np.zeros((2, 2)), np.zeros((2, 3)))


def test_combined_rule_uses_the_configured_weights():
    vv = np.array([[-4.0, -4.0]], dtype="float32")
    vh = np.array([[-8.0, -8.0]], dtype="float32")
    assert fb.combine_polarizations(vv, vh, 0.5, 0.5)[0, 0] == pytest.approx(-6.0)
    assert fb.combine_polarizations(vv, vh, 1.0, 0.0)[0, 0] == pytest.approx(-4.0)
    assert fb.combine_polarizations(vv, vh, 0.0, 1.0)[0, 0] == pytest.approx(-8.0)
    # Nodata in one polarization means the pixel cannot vote.
    vh_nan = np.array([[np.nan, -8.0]], dtype="float32")
    combined = fb.combine_polarizations(vv, vh_nan, 0.5, 0.5)
    assert np.isnan(combined[0, 0])
    assert combined[0, 1] == pytest.approx(-6.0)
    with pytest.raises(ValueError):
        fb.combine_polarizations(vv, vh, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Step 4: Otsu threshold + configured fallback range
# ---------------------------------------------------------------------------
def test_otsu_split_of_a_bimodal_histogram_lands_between_the_modes():
    rng = np.random.default_rng(0)
    unchanged = rng.normal(0.0, 0.6, 12000)
    flooded = rng.normal(-5.0, 0.6, 4000)
    threshold, method = fb.otsu_threshold(np.concatenate([unchanged, flooded]))
    assert method == "otsu"
    assert -4.5 < threshold < -1.0
    assert app_config.flood.otsu_fallback_min_db <= threshold <= app_config.flood.otsu_fallback_max_db


def test_otsu_is_clamped_into_the_configured_fallback_range():
    # A two-delta histogram (a small very dark class, everything else at 0 dB)
    # leaves Otsu's between-class variance flat, so the split falls outside the
    # configured range and the nearest bound is used instead.
    values = np.concatenate([np.zeros(500), np.full(20, -12.0)])
    threshold, method = fb.otsu_threshold(values)
    assert method == "otsu_fallback"
    assert threshold == pytest.approx(app_config.flood.otsu_fallback_min_db)


def test_otsu_reports_no_data_when_nothing_can_be_split():
    threshold, method = fb.otsu_threshold(np.array([np.nan, np.nan]))
    assert method == "fallback_no_data"
    assert threshold == pytest.approx(
        0.5 * (app_config.flood.otsu_fallback_min_db + app_config.flood.otsu_fallback_max_db)
    )
    threshold, method = fb.otsu_threshold(np.array([-3.0]))
    assert method == "fallback_no_data"


def test_otsu_does_not_use_a_reference_flood_map():
    # The threshold is a pure function of the scene histogram: two different
    # scenes with the same histogram shape give the same threshold, and the
    # only bounds involved come from config.
    rng = np.random.default_rng(1)
    scene = np.concatenate([rng.normal(0.0, 0.5, 5000), rng.normal(-4.0, 0.5, 2000)])
    first = fb.otsu_threshold(scene)
    second = fb.otsu_threshold(scene * 1.0)
    assert first == second
    lo = app_config.flood.otsu_fallback_min_db
    hi = app_config.flood.otsu_fallback_max_db
    assert lo <= first[0] <= hi


# ---------------------------------------------------------------------------
# Step 5: probability ramp
# ---------------------------------------------------------------------------
def test_probability_is_a_monotone_bounded_ramp_around_the_threshold():
    threshold = -3.0
    scores = fb.probability_from_log_ratio(
        np.array([[threshold, threshold - 1.0, threshold + 1.0, 5.0]], dtype="float32"),
        threshold,
        scale_db=1.0,
    )
    assert scores[0, 0] == pytest.approx(0.5)
    assert scores[0, 1] == pytest.approx(1.0 / (1.0 + np.exp(-1.0)), abs=1e-6)
    assert scores[0, 2] < 0.5 < scores[0, 1]
    assert 0.0 <= scores.min() and scores.max() <= 1.0


def test_probability_rejects_a_non_positive_scale():
    with pytest.raises(ValueError):
        fb.probability_from_log_ratio(np.zeros((2, 2), dtype="float32"), -3.0, 0.0)


# ---------------------------------------------------------------------------
# End-to-end on synthetic arrays: the known flood patch is recovered
# ---------------------------------------------------------------------------
def test_known_flood_patch_is_recovered_and_counts_are_consistent():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    intersection = int((result.mask & truth).sum())
    union = int((result.mask | truth).sum())
    assert intersection / union > 0.8          # the patch is found
    assert not result.mask[0, 0]               # unchanged land is not flooded
    assert result.is_empty is False
    assert result.threshold_method in ("otsu", "otsu_fallback")
    assert result.provenance["threshold_db"] == result.threshold_db
    assert float(np.nanmax(result.probability)) > 0.5
    # Probability is zero wherever the mask is False, and [0,1] everywhere.
    assert np.all(result.probability[~result.mask] == 0.0)
    assert result.probability.min() >= 0.0 and result.probability.max() <= 1.0

    counts = result.counts
    assert counts["grid_pixels"] == truth.size
    assert counts["valid_overlap_pixels"] == truth.size
    assert counts["candidate_pixels"] >= counts["final_mask_pixels"] == int(result.mask.sum())
    removed = (
        counts["excluded_permanent_water"]
        + counts["excluded_slope"]
        + counts["excluded_radar_shadow"]
        + counts["excluded_hand"]
        + counts["excluded_small_components"]
    )
    assert counts["candidate_pixels"] - removed == counts["final_mask_pixels"]


def test_single_pre_scene_and_a_median_of_three_agree_on_a_stable_scene():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    single = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    # Two noisy extra pre scenes; the median suppresses them.
    noisy_vv = pre_vv + 0.4
    noisy_vh = pre_vh - 0.4
    triple = fb.build_baseline_flood_map(
        post_vv, post_vh, [noisy_vv, pre_vv, noisy_vh * 0 + pre_vh],
        [noisy_vh, pre_vh, noisy_vv * 0 + pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    assert triple.provenance["pre_scenes_used"] == 3
    assert triple.provenance["median_pre"] is True
    assert triple.counts["final_mask_pixels"] > 0
    assert abs(triple.counts["final_mask_pixels"] - single.counts["final_mask_pixels"]) < 20


def test_empty_input_returns_an_empty_map_instead_of_a_guess():
    empty = np.full((10, 10), np.nan, dtype="float32")
    result = fb.build_baseline_flood_map(
        empty, empty, [empty], [empty], bbox=TRISHULI_BBOX,
        transform=_grid_transform((10, 10)),
    )
    assert result.is_empty
    assert result.counts["valid_overlap_pixels"] == 0
    assert result.counts["final_mask_pixels"] == 0
    assert result.area_km2 == 0.0
    assert result.polygons["features"] == []
    assert result.threshold_db is None and result.threshold_method == "not_applicable"
    assert result.provenance["no_valid_overlap"] is True
    assert float(result.probability.max()) == 0.0


def test_grid_mismatch_and_too_many_pre_scenes_are_rejected():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    with pytest.raises(ValueError):
        fb.build_baseline_flood_map(
            post_vv, post_vh, [pre_vv[:, :-1]], [pre_vh[:, :-1]], bbox=TRISHULI_BBOX
        )
    too_many = [pre_vv] * (app_config.pairing.max_pre_scenes + 1)
    with pytest.raises(ValueError):
        fb.build_baseline_flood_map(
            post_vv, post_vh, too_many, [pre_vh] * len(too_many), bbox=TRISHULI_BBOX
        )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------
def test_permanent_water_is_excluded_from_new_flooding():
    shape = (30, 30)
    pre_vv = np.full(shape, -10.0, dtype="float32")
    pre_vh = np.full(shape, -16.0, dtype="float32")
    # A permanent lake (dark in both polarizations before the event) ...
    pre_vv[20:26, :] = -18.0
    pre_vh[20:26, :] = -24.0
    post_vv = pre_vv.copy()
    post_vh = pre_vh.copy()
    # ... and a new flood that overlaps the lake plus fresh land.
    post_vv[18:26, 5:12] -= 8.0
    post_vh[18:26, 5:12] -= 9.0
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(shape),
    )
    lake = water_mask.permanent_water_mask(pre_vv, pre_vh)
    assert lake.sum() == 6 * 30
    assert result.counts["excluded_permanent_water"] > 0
    assert not (result.mask & lake).any()          # the lake itself is not "new"
    assert result.mask[19, 7]                      # fresh flooding next to it is kept


def test_permanent_water_requires_both_polarizations_to_be_dark():
    # Dark in VV only (e.g. a smooth dry surface) is not permanent water.
    dark_vv_only = water_mask.permanent_water_mask(
        np.array([[-18.0]], dtype="float32"), np.array([[-8.0]], dtype="float32")
    )
    assert dark_vv_only.item() is False
    both_dark = water_mask.permanent_water_mask(
        np.array([[-18.0]], dtype="float32"), np.array([[-24.0]], dtype="float32")
    )
    assert both_dark.item() is True
    nodata = water_mask.permanent_water_mask(
        np.array([[np.nan]], dtype="float32"), np.array([[-24.0]], dtype="float32")
    )
    assert nodata.item() is False


def test_steep_pixels_are_excluded_from_the_flood_mask():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    slope = np.zeros(truth.shape, dtype="float64")
    steep = np.zeros(truth.shape, dtype=bool)
    steep[8:14, 12:16] = True                 # half the patch sits on a steep face
    slope[steep] = app_config.terrain.slope_cutoff_degrees + 10.0
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape), slope_deg=slope,
    )
    assert result.counts["excluded_slope"] > 0
    assert not (result.mask & steep).any()
    assert (result.mask & truth).any()        # the gentle part survives


def test_radar_shadow_pixels_are_excluded_and_counted():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    shadow = np.zeros(truth.shape, dtype=bool)
    shadow[8:10, 8:16] = True
    baseline = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape), shadow_mask=shadow,
    )
    assert not (result.mask & shadow).any()
    # Every candidate pixel inside the shadow is removed and counted (the
    # speckle filter trims the corners of the synthetic patch, so the count is
    # bounded by the shadow patch size rather than exactly equal to it).
    assert 0 < result.counts["excluded_radar_shadow"] <= int(shadow.sum())
    assert result.counts["candidate_pixels"] == baseline.counts["candidate_pixels"]
    assert result.counts["final_mask_pixels"] < baseline.counts["final_mask_pixels"]


def test_terrain_unknowns_are_counted_and_not_excluded():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    slope = np.zeros(truth.shape, dtype="float64")
    slope[0:5, 0:5] = np.nan
    hand = np.zeros(truth.shape, dtype="float64")
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape), slope_deg=slope, hand_m=hand,
    )
    assert result.provenance["terrain_unknown_pixels"]["slope"] == 25
    assert result.counts["final_mask_pixels"] > 0


def test_slope_computation_matches_a_known_ramp_and_flags_steep_ground():
    # 45 degree ramp: 1 m rise per 1 m east.
    dem = np.tile(np.arange(20, dtype="float64"), (5, 1))
    slope = terrain.compute_slope_degrees(dem, x_m=1.0, y_m=1.0)
    assert slope[2, 10] == pytest.approx(45.0, abs=1e-6)
    assert terrain.slope_exclusion_mask(slope, 15.0).all()
    flat = np.zeros((5, 5), dtype="float64")
    assert terrain.compute_slope_degrees(flat, 30.0, 30.0).max() == pytest.approx(0.0)
    assert not terrain.slope_exclusion_mask(terrain.compute_slope_degrees(flat, 30.0, 30.0), 15.0).any()


def test_shadow_mask_projects_the_gradient_onto_the_range_axis():
    # A face descending steeply to the east is in shadow/layover for an
    # east-looking pass, but a face descending to the north is not.
    # 2 m drop per 1 m eastward: ~63 degrees, beyond the 52 degree cutoff.
    east_down = np.tile(np.arange(20, dtype="float64")[::-1] * 2.0, (5, 1))
    assert terrain.compute_shadow_layover_mask(
        east_down, 1.0, 1.0, look_azimuth_deg=90.0, shadow_slope_deg=52.0
    ).any()
    north_down = np.tile(np.arange(20, dtype="float64")[:, None], (1, 5))  # drops northward
    assert not terrain.compute_shadow_layover_mask(
        north_down, 1.0, 1.0, look_azimuth_deg=90.0, shadow_slope_deg=52.0
    ).any()
    # A gentle slope is never flagged.
    gentle = np.tile(np.arange(5, dtype="float64") * 0.1, (5, 1))
    assert not terrain.compute_shadow_layover_mask(
        gentle, 1.0, 1.0, look_azimuth_deg=90.0, shadow_slope_deg=52.0
    ).any()


def test_look_azimuth_follows_the_orbit_direction_from_config():
    assert terrain.radar_look_azimuth_deg("ASCENDING") == app_config.flood.s1_look_azimuth_ascending_deg
    assert terrain.radar_look_azimuth_deg("DESCENDING") == app_config.flood.s1_look_azimuth_descending_deg


def test_flow_accumulation_and_hand_on_a_known_drainage():
    # Rows increase southward, so this plane rises to the south and every column
    # drains north toward row 0.
    dem = np.tile(np.arange(10, dtype="float64")[:, None] * 10.0, (1, 5))
    accumulation = terrain.flow_accumulation_d8(dem, x_m=30.0, y_m=30.0)
    assert accumulation[0, 0] == 10             # the outlet collects the column
    assert accumulation[-1, 0] == 1             # the ridge drains only itself
    assert accumulation[5, 0] == 5

    hand = terrain.compute_hand(dem, accumulation_threshold=5, x_m=30.0, y_m=30.0)
    assert hand[0, 0] == pytest.approx(0.0)     # drainage cells
    assert hand[5, 0] == pytest.approx(0.0)
    assert hand[6, 0] == pytest.approx(10.0)    # one cell (10 m) above the drainage
    assert hand[9, 0] == pytest.approx(40.0)
    mask = terrain.hand_exclusion_mask(hand, 15.0)
    assert mask[7, 0] and mask[9, 0]
    assert not mask[6, 0]                        # close to the drainage: kept

    # With a threshold no river reaches, HAND is undefined rather than zero.
    assert np.isnan(terrain.compute_hand(dem, accumulation_threshold=1000)).all()


def test_hand_excludes_pixels_far_above_the_drainage_in_the_flood_map():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    hand = np.zeros(truth.shape, dtype="float64")
    hand[8:10, 8:16] = app_config.terrain.hand_cutoff_m + 5.0
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape), hand_m=hand,
    )
    assert result.counts["excluded_hand"] > 0
    assert not (result.mask & (hand > app_config.terrain.hand_cutoff_m)).any()


def test_apply_terrain_masks_keeps_its_existing_behaviour():
    probability = np.ones((3, 3), dtype="float32")
    slope = np.zeros((3, 3)); slope[0, 0] = 20.0
    hand = np.zeros((3, 3)); hand[1, 1] = 30.0
    out = terrain.apply_terrain_masks(probability, slope, hand, 15.0, 15.0)
    assert out[0, 0] == 0.0 and out[1, 1] == 0.0 and out[2, 2] == 1.0


def test_exclude_permanent_water_keeps_its_existing_behaviour():
    probability = np.ones((2, 2), dtype="float32")
    permanent = np.zeros((2, 2)); permanent[0, 1] = 1
    out = water_mask.exclude_permanent_water(probability, permanent)
    assert out[0, 1] == 0.0 and out[1, 1] == 1.0


# ---------------------------------------------------------------------------
# Step 7: cleaning
# ---------------------------------------------------------------------------
def test_label_components_and_min_component_size_cleaning():
    mask = np.zeros((10, 10), dtype=bool)
    mask[1:4, 1:4] = True          # 9 pixels
    mask[7, 7] = True              # 1 pixel speckle
    labels, count = fb.label_components(mask)
    assert count == 2
    assert labels[2, 2] != labels[7, 7]

    cleaned = fb.clean_binary_mask(mask, min_component_size=9)
    assert cleaned.sum() == 9
    assert not cleaned[7, 7]
    # min_component_size=1 (or 0) keeps everything.
    assert fb.clean_binary_mask(mask, min_component_size=1).sum() == 10


def test_small_speckle_components_are_dropped_by_the_pipeline(monkeypatch):
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    post_vv[34:38, 34:38] -= 9.0   # a small dark blob, not a real flood extent
    post_vh[34:38, 34:38] -= 9.0
    default = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    assert default.mask[10, 12]                     # the real patch is detected
    # With the configured minimum raised above the blob size it becomes a
    # speckle remnant and is dropped, while the large patch survives.
    monkeypatch.setattr(app_config.flood, "min_component_size_px", 32)
    cleaned = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    assert not cleaned.mask[35, 35]
    assert cleaned.counts["excluded_small_components"] > 0
    assert cleaned.counts["final_mask_pixels"] == int(cleaned.mask.sum())
    assert cleaned.counts["final_mask_pixels"] < default.counts["final_mask_pixels"]


# ---------------------------------------------------------------------------
# Step 8: polygons, area, writers
# ---------------------------------------------------------------------------
def _assert_valid_geojson(feature_collection, bbox, expected_pixels=None):
    assert feature_collection["type"] == "FeatureCollection"
    assert "EPSG::4326" in feature_collection["crs"]["properties"]["name"]
    assert feature_collection["features"], "expected at least one polygon"
    min_lon, min_lat, max_lon, max_lat = bbox
    component_pixels = {}
    for feature in feature_collection["features"]:
        assert feature["type"] == "Feature"
        geometry = feature["geometry"]
        assert geometry["type"] in ("Polygon", "MultiPolygon")
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for polygon in polygons:
            for ring in polygon:
                assert len(ring) >= 4
                assert ring[0] == ring[-1]              # rings are closed
                for lon, lat in ring:
                    assert min_lon - 1e-6 <= lon <= max_lon + 1e-6
                    assert min_lat - 1e-6 <= lat <= max_lat + 1e-6
        # A component with holes or several parts yields several features that
        # all carry the whole component's pixel count; count each component once.
        component_pixels[int(feature["properties"]["component"])] = int(
            feature["properties"]["pixel_count"]
        )
    pixel_total = sum(component_pixels.values())
    if expected_pixels is not None:
        assert pixel_total == expected_pixels
    return pixel_total


def test_polygons_are_valid_geojson_in_epsg4326():
    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    _assert_valid_geojson(result.polygons, TRISHULI_BBOX, expected_pixels=int(result.mask.sum()))


def test_empty_mask_produces_no_polygons():
    mask = np.zeros((5, 5), dtype=bool)
    polygons = fb.mask_to_geojson(mask, _grid_transform((5, 5)))
    assert polygons["features"] == []


def test_area_is_measured_in_the_utm_zone_of_the_aoi_centroid():
    shape = (1000, 1115)                     # ~20 m pixels over the preset AOI
    mask = np.zeros(shape, dtype=bool)
    mask[100:200, 100:200] = True            # 10 000 pixels
    area_km2, meta = fb.mask_area_km2(mask, TRISHULI_BBOX)
    assert meta["utm_epsg"] == 32645         # zone 45N from the centroid
    assert meta["utm_epsg"] == geo.get_utm_epsg_for_bbox(TRISHULI_BBOX)
    assert geo.get_utm_epsg_for_bbox([85.15, -28.05, 85.35, -27.85]) == 32745
    # 10 000 pixels at ~20 m is ~4 km2; the single-UTM approximation stays
    # within a few percent of the lon/lat pixel area.
    assert 3.5 < area_km2 < 4.3
    assert meta["pixels"] == 10000


def test_write_flood_artifacts_round_trips(tmp_path):
    import rasterio

    pre_vv, pre_vh, post_vv, post_vh, truth = _scene_pair()
    result = fb.build_baseline_flood_map(
        post_vv, post_vh, [pre_vv], [pre_vh], bbox=TRISHULI_BBOX,
        transform=_grid_transform(truth.shape),
    )
    paths = fb.write_flood_artifacts(result, tmp_path)
    for path in paths.values():
        assert Path(path).is_file()
    with rasterio.open(paths["mask"]) as dataset:
        assert dataset.read(1).sum() == result.mask.sum()
        assert dataset.crs.to_string() == "EPSG:4326"
    with rasterio.open(paths["probability"]) as dataset:
        probability = dataset.read(1)
        assert probability.shape == result.shape
        assert 0.0 <= float(probability.min()) and float(probability.max()) <= 1.0
    polygons = json.loads(Path(paths["polygons"]).read_text(encoding="utf-8"))
    _assert_valid_geojson(polygons, TRISHULI_BBOX, expected_pixels=int(result.mask.sum()))
    steps = json.loads(Path(paths["steps"]).read_text(encoding="utf-8"))
    assert steps["counts"] == result.counts
    assert steps["threshold_method"] == result.threshold_method


def test_resample_nearest_onto_another_grid():
    source = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    source_transform = fb.grid_transform([0.0, 0.0, 2.0, 2.0], (2, 2))
    target_transform = fb.grid_transform([0.0, 0.0, 2.0, 2.0], (4, 4))
    out = fb.resample_nearest(source, source_transform, target_transform, (4, 4))
    assert out.shape == (4, 4)
    assert out[0, 0] == pytest.approx(1.0)   # target row 0 is the northern edge
    assert out[3, 3] == pytest.approx(4.0)   # target row 3 is the southern edge
    assert out[0, 1] == pytest.approx(1.0) and out[2, 0] == pytest.approx(3.0)
    # A target outside the source extent is filled with NaN.
    shifted = fb.grid_transform([10.0, 10.0, 12.0, 12.0], (4, 4))
    assert np.isnan(fb.resample_nearest(source, source_transform, shifted, (4, 4))).all()


# ---------------------------------------------------------------------------
# Refusal path (no network needed: the pairing rule runs before any fetch)
# ---------------------------------------------------------------------------
def test_no_valid_pair_refuses_and_never_produces_a_map():
    post = {
        "scene_id": "post", "acquisition_time": "2026-08-28T00:10:37Z",
        "orbit_direction": "ASCENDING", "relative_orbit": 85,
    }
    # Same orbit but the wrong pass direction: not a valid pair.
    wrong_direction = [
        {"scene_id": "pre", "acquisition_time": "2026-08-16T12:21:41Z",
         "orbit_direction": "DESCENDING", "relative_orbit": 85}
    ]
    with pytest.raises(NoValidOrbitPairError):
        fb.fetch_and_build_flood_map(post, wrong_direction, bbox=TRISHULI_BBOX)
    # No pre scene at all: also a refusal.
    with pytest.raises(NoValidOrbitPairError):
        fb.fetch_and_build_flood_map(post, [], bbox=TRISHULI_BBOX)


# ---------------------------------------------------------------------------
# Integration: live Trishuli preset, structural properties only
# ---------------------------------------------------------------------------
needs_cdse = pytest.mark.skipif(
    not (settings.cdse_client_id and settings.cdse_client_secret),
    reason="CDSE credentials required for the live baseline flood map",
)


def _trishuli_preset():
    with open(REPO_ROOT / "config" / "presets" / "trishuli_emsr927.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.integration
@needs_cdse
def test_trishuli_baseline_flood_map_structure():
    """Live end-to-end run: only structural properties are asserted.

    No comparison against EMSR927 or any published flood map: the assertions are
    that a map is produced at all, that the area sits inside the configured
    sanity range, that the polygons are valid GeoJSON, and that the step counts
    add up.
    """
    from datetime import date, timedelta

    from app.pipeline import cdse

    preset = _trishuli_preset()
    bbox = [float(c) for c in preset["bbox"]]
    event_date = str(preset["event_date"])
    search_start = (
        date.fromisoformat(event_date)
        - timedelta(days=app_config.pairing.search_window_days)
    ).isoformat()
    search_end = (date.fromisoformat(event_date) + timedelta(days=7)).isoformat()

    scenes = cdse.search_sentinel1_scenes(bbox, search_start, search_end, instrument_mode="IW")
    assert scenes, "no Sentinel-1 scenes in the search window"
    posts = [s for s in scenes if str(s["acquisition_time"])[:10] >= event_date]
    pres = [s for s in scenes if str(s["acquisition_time"])[:10] < event_date]
    assert posts and pres, "expected both a post-event and a pre-event acquisition"
    post_scene = min(posts, key=lambda s: s["acquisition_time"])

    result = fb.fetch_and_build_flood_map(post_scene, pres, bbox=bbox)

    assert result.shape == (result.mask.shape[0], result.mask.shape[1])
    assert result.counts["valid_overlap_pixels"] > 0
    assert result.is_empty is False, "baseline flood map is empty (unexpected)"
    assert result.threshold_method in ("otsu", "otsu_fallback")
    lo = app_config.flood.area_sanity_min_km2
    hi = app_config.flood.area_sanity_max_km2
    assert lo <= result.area_km2 <= hi, f"area {result.area_km2} km2 outside [{lo}, {hi}]"
    assert result.utm_epsg == geo.get_utm_epsg_for_bbox(bbox)
    _assert_valid_geojson(result.polygons, bbox, expected_pixels=int(result.mask.sum()))
    counts = result.counts
    removed = (
        counts["excluded_permanent_water"]
        + counts["excluded_slope"]
        + counts["excluded_radar_shadow"]
        + counts["excluded_hand"]
        + counts["excluded_small_components"]
    )
    assert counts["candidate_pixels"] - removed == counts["final_mask_pixels"]
    assert result.provenance["scene_ids"]["post"] == str(post_scene["scene_id"])
    assert result.provenance["pre_scenes_used"] >= 1
