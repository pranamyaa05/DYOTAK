"""Tests for the G0 spike pairing assertions (scripts/g0_spike.py)."""

import importlib.util
import io
import sys
from pathlib import Path

import numpy as np
import pytest
import tifffile


def _load_spike():
    path = Path(__file__).resolve().parents[2] / "scripts" / "g0_spike.py"
    spec = importlib.util.spec_from_file_location("g0_spike", path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects sys.modules[cls.__module__], so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def _scene(scene_id, when, direction="DESCENDING", orbit=19):
    return {
        "scene_id": scene_id,
        "acquisition_time": when,
        "orbit_direction": direction,
        "relative_orbit": orbit,
    }


def _s2(scene_id, when, cloud_pct):
    return {"scene_id": scene_id, "acquisition_time": when, "cloud_pct": cloud_pct}


def test_pairing_report_picks_expected_pre_post_and_gap():
    scenes = [
        _scene("pre", "2024-09-20T00:25:11Z"),
        _scene("post", "2024-10-02T00:19:42Z"),
        _scene("older-pre", "2024-09-08T00:25:11Z"),
    ]
    report = spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
    assert report["post_date"] == "2024-10-02"
    assert report["pre_date"] == "2024-09-20"
    assert report["gap_days"] == 12
    assert report["expected_days"] == 12
    assert report["orbit"] == 19
    assert report["direction"] == "DESCENDING"


def test_pairing_report_picks_first_scene_after_event():
    scenes = [
        _scene("pre", "2024-09-17T00:25:11Z"),
        _scene("first-post", "2024-09-29T00:19:42Z"),  # 1 day after event
        _scene("later-post", "2024-10-02T00:19:42Z"),
    ]
    report = spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
    assert report["post_date"] == "2024-09-29"


def test_pairing_report_rejects_wrong_gap():
    scenes = [
        _scene("pre", "2024-09-01T00:25:11Z"),   # 31 days before post
        _scene("post", "2024-10-02T00:19:42Z"),
    ]
    with pytest.raises(ValueError):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)


def test_pairing_report_rejects_no_post():
    scenes = [_scene("pre", "2024-09-20T00:25:11Z")]
    with pytest.raises(ValueError):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)


def test_pairing_report_rejects_no_matching_pre_orbit():
    scenes = [
        _scene("pre-asc", "2024-09-20T00:25:11Z", direction="ASCENDING", orbit=19),
        _scene("post", "2024-10-02T00:19:42Z", direction="DESCENDING", orbit=19),
    ]
    # No same-direction pre scene -> pairing rule raises (AssertionError/NoValidOrbitPairError)
    with pytest.raises(Exception):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)


def _tiff_bytes(arr) -> bytes:
    buf = io.BytesIO()
    # Pin the interpretation so the on-disk layout matches the array shape.
    tifffile.imwrite(buf, arr.astype("float32"), photometric="minisblack")
    return buf.getvalue()


def test_s1_clip_band_stats_uses_data_mask_and_floor_as_nodata():
    # VV/VH plus the explicit dataMask band the evalscript now returns.
    vv = np.full((4, 4), -12.0, dtype="float32")
    vh = np.full((4, 4), -20.0, dtype="float32")
    mask = np.ones((4, 4), dtype="float32")
    vv[0, 0] = float("nan")           # 1 of 16 non-finite
    vv[1, 1] = spike.cdse.S1_DB_FLOOR  # at the floor -> nodata
    mask[2, 2] = 0.0                   # masked out -> nodata
    stats = spike.s1_clip_band_stats(_tiff_bytes(np.dstack([vv, vh, mask])))

    assert [s["name"] for s in stats] == ["VV", "VH"]
    vv_stats, vh_stats = stats
    # 3 of 16 pixels are nodata: NaN, at-floor and masked.
    assert vv_stats["nodata_pct"] == pytest.approx(3 / 16 * 100)
    assert vv_stats["nonfinite_pct"] == pytest.approx(1 / 16 * 100)
    assert vv_stats["p1"] == pytest.approx(-12.0)
    assert vv_stats["p50"] == pytest.approx(-12.0)
    assert vv_stats["p99"] == pytest.approx(-12.0)
    assert vv_stats["shape"] == (4, 4)
    # VH shares the mask: only the masked pixel is nodata.
    assert vh_stats["nodata_pct"] == pytest.approx(1 / 16 * 100)
    assert vh_stats["nonfinite_pct"] == pytest.approx(0.0)
    assert vh_stats["p50"] == pytest.approx(-20.0)


def test_s1_clip_band_stats_reports_share_above_plus_10_db():
    vv = np.full((4, 4), -14.0, dtype="float32")
    vv[0, 0] = 25.0  # 1 of 16 valid pixels above +10 dB
    vh = np.full((4, 4), -20.0, dtype="float32")
    mask = np.ones((4, 4), dtype="float32")
    stats = spike.s1_clip_band_stats(_tiff_bytes(np.dstack([vv, vh, mask])))
    assert stats[0]["above10_pct"] == pytest.approx(1 / 16 * 100)
    assert stats[0]["p99"] > 10.0
    assert stats[1]["above10_pct"] == pytest.approx(0.0)


def test_s1_clip_band_stats_handles_band_first_layout():
    # Rectangular dims so the (B, H, W) layout is unambiguous.
    vv = np.full((4, 5), -15.0, dtype="float32")
    vh = np.full((4, 5), -22.0, dtype="float32")
    mask = np.ones((4, 5), dtype="float32")
    stats = spike.s1_clip_band_stats(_tiff_bytes(np.stack([vv, vh, mask])))
    assert [s["name"] for s in stats] == ["VV", "VH"]
    assert all(s["nodata_pct"] == pytest.approx(0.0) for s in stats)
    assert stats[1]["p50"] == pytest.approx(-22.0)


def test_s1_clip_band_stats_handles_legacy_two_band_clip():
    vv = np.full((3, 3), -15.0, dtype="float32")
    vh = np.full((3, 3), -22.0, dtype="float32")
    stats = spike.s1_clip_band_stats(_tiff_bytes(np.dstack([vv, vh])))
    assert [s["name"] for s in stats] == ["VV", "VH"]
    assert all(s["nodata_pct"] == pytest.approx(0.0) for s in stats)


def test_s1_stats_warn_thresholds():
    clean = [{"name": "VV", "nodata_pct": 0.4, "nonfinite_pct": 0.0},
             {"name": "VH", "nodata_pct": 1.0, "nonfinite_pct": 0.0}]
    assert spike.s1_stats_should_warn(clean) is None

    too_much_nodata = [dict(clean[0], nodata_pct=5.5), clean[1]]
    reason = spike.s1_stats_should_warn(too_much_nodata)
    assert reason is not None and "nodata" in reason

    too_many_nan = [dict(clean[0], nonfinite_pct=1.5), clean[1]]
    reason = spike.s1_stats_should_warn(too_many_nan)
    assert reason is not None and "non-finite" in reason


def test_runner_warn_renders_and_does_not_fail_the_run(capsys):
    runner = spike.Runner()
    runner.run("warned", lambda: (spike.WARN, "too much nodata"))
    runner.run("fine", lambda: (spike.PASS, "ok"))
    assert runner.render() == 0  # WARN is not a failure
    out = capsys.readouterr().out
    assert "1 warned" in out and "0 failed" in out

    failing = spike.Runner()
    failing.run("broken", lambda: (spike.FAIL, "nope"))
    assert failing.render() == 1


def test_select_s2_scene_rule_is_clearest_post_event_then_overall():
    scenes = [
        _s2("pre-clear", "2026-08-11T04:52:41Z", 13.37),
        _s2("post-cloudy", "2026-08-27T04:56:59Z", 54.29),
        _s2("post-clear", "2026-09-02T05:00:00Z", 21.5),
    ]
    chosen = spike.select_s2_scene(scenes, "2026-08-26")
    assert chosen["scene_id"] == "post-clear"
    # With no post-event scene, fall back to the clearest overall.
    chosen = spike.select_s2_scene(scenes[:1], "2026-08-26")
    assert chosen["scene_id"] == "pre-clear"
    assert spike.select_s2_scene([], "2026-08-26") is None


def test_pairing_report_pre_scene_carries_the_fields_fetch_needs():
    scenes = [
        _scene("pre", "2024-09-20T00:25:11Z"),
        _scene("post", "2024-10-02T00:19:42Z"),
    ]
    report = spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
    pre = report["pre_scene"]
    assert pre["scene_id"] == "pre"
    assert pre["orbit_direction"] == "DESCENDING"
    # The reported pre scene is used as-is to build the clip request.
    req = spike.cdse.build_s1_process_request(
        [85.15, 27.85, 85.35, 28.05], pre["acquisition_time"], 4, 4,
        orbit_direction=pre["orbit_direction"],
    )
    assert req["input"]["data"][0]["dataFilter"]["orbitDirection"] == "DESCENDING"


def test_s1_log_ratio_is_near_zero_for_identical_clips():
    band = np.full((4, 4), -12.0, dtype="float32")
    mask = np.ones((4, 4), dtype="float32")
    data = _tiff_bytes(np.dstack([band, band, mask]))
    stats = spike.s1_log_ratio_stats(data, data)
    assert [s["name"] for s in stats] == ["VV", "VH"]
    for band_stats in stats:
        assert band_stats["overlap_pct"] == pytest.approx(100.0)
        assert band_stats["p1"] == pytest.approx(0.0)
        assert band_stats["p50"] == pytest.approx(0.0)
        assert band_stats["p99"] == pytest.approx(0.0)
        assert band_stats["below_drop_pct"] == pytest.approx(0.0)
        assert band_stats["floor_post_pct"] == pytest.approx(0.0)
        assert band_stats["floor_pre_pct"] == pytest.approx(0.0)
    assert spike.s1_log_ratio_should_warn(stats) is None


def test_s1_log_ratio_counts_drops_and_excludes_nodata_pixels():
    pre = np.full((4, 4), -10.0, dtype="float32")
    post = np.full((4, 4), -10.0, dtype="float32")
    post[0, 0] = -20.0                    # -10 dB drop
    post[0, 1] = -11.0                    # -1 dB, above the -3 dB threshold
    post[0, 2] = spike.cdse.S1_DB_FLOOR   # at the floor -> not usable
    pre[0, 3] = float("nan")              # non-finite in pre -> not usable
    mask = np.ones((4, 4), dtype="float32")
    stats = spike.s1_log_ratio_stats(
        _tiff_bytes(np.dstack([post, post, mask])),
        _tiff_bytes(np.dstack([pre, pre, mask])),
    )
    vv = stats[0]
    # 2 of 16 pixels are unusable in one clip or the other (floor / non-finite).
    assert vv["overlap_pct"] == pytest.approx(14 / 16 * 100)
    assert vv["below_drop_pct"] == pytest.approx(1 / 14 * 100)
    assert vv["p1"] < -3.0
    assert vv["p50"] == pytest.approx(0.0)  # the bulk of the AOI is unchanged
    assert vv["floor_post_pct"] == pytest.approx(1 / 16 * 100)
    assert vv["floor_pre_pct"] == pytest.approx(0.0)


def test_s1_log_ratio_rejects_mismatched_clips():
    a = _tiff_bytes(np.dstack([np.zeros((4, 4), dtype="float32"),
                               np.zeros((4, 4), dtype="float32")]))
    other_shape = _tiff_bytes(np.dstack([np.zeros((4, 5), dtype="float32"),
                                         np.zeros((4, 5), dtype="float32")]))
    with pytest.raises(ValueError):
        spike.s1_log_ratio_stats(a, other_shape)
    one_band = _tiff_bytes(np.zeros((4, 4), dtype="float32"))
    with pytest.raises(ValueError):
        spike.s1_log_ratio_stats(a, one_band)


def test_s1_log_ratio_warns_when_the_median_is_far_from_zero():
    stats = [
        {"name": "VV", "p1": 2.0, "p50": 2.5, "p99": 3.0},
        {"name": "VH", "p1": -0.2, "p50": 0.1, "p99": 0.3},
    ]
    assert spike.s1_log_ratio_should_warn([stats[1]]) is None
    reason = spike.s1_log_ratio_should_warn(stats)
    assert reason is not None
    assert "VV" in reason and "2.50" in reason
    # A band with no overlapping pixels cannot be checked: report it.
    no_overlap = [{"name": "VV", "p1": float("nan"), "p50": float("nan"),
                   "p99": float("nan")}]
    assert spike.s1_log_ratio_should_warn(no_overlap) is not None


def _georeferenced_tiff(array, transform):
    import rasterio

    buf = io.BytesIO()
    with rasterio.open(
        buf, "w", driver="GTiff", height=array.shape[0], width=array.shape[1],
        count=1, dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(array.astype("float32"), 1)
    return buf.getvalue()


def test_s1_clip_grid_reads_shape_and_bounds():
    from rasterio.transform import from_origin

    data = _georeferenced_tiff(
        np.full((4, 5), -12.0), from_origin(85.15, 28.05, 0.01, 0.01)
    )
    grid = spike.s1_clip_grid(data)
    assert grid["shape"] == (4, 5)
    # (left, bottom, right, top)
    assert grid["bounds"] == pytest.approx((85.15, 28.01, 85.20, 28.05), abs=1e-6)
    assert grid["crs"] == "EPSG:4326"


def test_grids_match_requires_one_shared_grid():
    grid = {"shape": (4, 5), "bounds": (85.15, 28.01, 85.20, 28.05), "crs": "EPSG:4326"}
    assert spike.grids_match(grid, dict(grid)) is None
    noisy = {"shape": (4, 5), "bounds": (85.15 + 1e-9, 28.01, 85.20, 28.05)}
    assert spike.grids_match(grid, noisy) is None
    assert "shape" in spike.grids_match(grid, {"shape": (5, 5), "bounds": grid["bounds"]})
    assert "bounds" in spike.grids_match(
        grid, {"shape": (4, 5), "bounds": (85.15, 28.01, 85.20, 28.06)}
    )


def test_ohsome_extract_data_timestamp_reads_newest_parquet(tmp_path):
    import datetime as dt

    import pyarrow as pa
    import pyarrow.parquet as pq

    def _epoch_micros(year, month, day, hour, minute, second):
        moment = dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)
        return int(moment.timestamp() * 1_000_000)

    def _parquet(path, micros):
        pq.write_table(
            pa.table({"edit_timestamp": pa.array([micros], type=pa.timestamp("us", tz="UTC"))}),
            path,
        )

    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    _parquet(a, _epoch_micros(2026, 3, 9, 23, 59, 23))
    _parquet(b, _epoch_micros(2026, 1, 2, 0, 0, 0))
    stamp = spike.ohsome_extract_data_timestamp(
        {"parquet_paths": {"building": str(a), "highway": str(b)}}
    )
    assert stamp == "2026-03-09T23:59:23Z"
    assert spike.ohsome_extract_data_timestamp({}) is None
