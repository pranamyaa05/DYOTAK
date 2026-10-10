"""Unit tests for app.pipeline.cdse (no network; helpers and cache only)."""

import datetime as dt
import io

import numpy as np
import pytest
import tifffile

from app.common.errors import CdseAuthError, CdseUnavailableError
from app.pipeline import cdse
from app.settings import app_config

BBOX = [85.15, 27.85, 85.35, 28.05]
ACQ = "2024-09-29T00:25:11Z"

S1_FEATURE = {
    "id": "S1A_IW_GRDH_1SDV_20240929T002511_20240929T002536_055871_06D14A_83B2",
    "type": "Feature",
    "properties": {
        "datetime": ACQ,
        "sat:orbit_state": "descending",
        "sat:relative_orbit": 121,
        "sar:instrument_mode": "IW",
        "product:type": "IW_GRDH_1S",
        "platform": "sentinel-1a",
    },
}


# --- pure geometry / date helpers -------------------------------------------
def test_bbox_to_output_size_is_positive():
    w, h = cdse._bbox_to_output_size(BBOX, 10.0)
    assert w > 0 and h > 0
    # ~0.2 deg lon at 28N is ~19.6 km, ~0.2 deg lat ~22.3 km.
    assert 1800 <= w <= 2100
    assert 2000 <= h <= 2400


def test_bbox_to_output_size_caps_large_aoi():
    w, h = cdse._bbox_to_output_size([-20, 10, 20, 40], 10.0)
    assert max(w, h) <= cdse.MAX_CLIP_PIXELS


def test_bbox_to_output_size_rejects_bad_resolution():
    with pytest.raises(ValueError):
        cdse._bbox_to_output_size(BBOX, 0)


def test_iso_plus_seconds():
    assert cdse._iso_plus_seconds(ACQ, 60) == "2024-09-29T00:26:11Z"
    assert cdse._date_only("2024-09-29T00:25:11Z") == "2024-09-29"


# --- catalog normalization --------------------------------------------------
def test_normalize_s1_item():
    scene = cdse._normalize_s1_item(S1_FEATURE)
    assert scene["scene_id"] == S1_FEATURE["id"]
    assert scene["acquisition_time"] == ACQ
    assert scene["orbit_direction"] == "DESCENDING"
    assert scene["relative_orbit"] == 121


def test_normalize_s1_item_missing_direction():
    feature = {"id": "x", "properties": {"datetime": ACQ}}
    scene = cdse._normalize_s1_item(feature)
    assert scene["orbit_direction"] is None
    assert scene["relative_orbit"] is None


def test_normalize_s2_item():
    feature = {
        "id": "S2A_MSIL2A_x",
        "properties": {"datetime": ACQ, "eo:cloud_cover": 12.4, "sat:relative_orbit": 90},
    }
    scene = cdse._normalize_s2_item(feature)
    assert scene["cloud_pct"] == 12.4
    assert scene["relative_orbit"] == 90


# --- process request builders ----------------------------------------------
def test_build_s1_process_request():
    req = cdse.build_s1_process_request(BBOX, ACQ, 100, 120, orbit_direction="DESCENDING")
    data = req["input"]["data"][0]
    assert data["type"] == "sentinel-1-grd"
    assert data["processing"]["orthorectify"] is True
    assert data["processing"]["demInstance"] == "COPERNICUS_30"
    assert data["processing"]["backCoeff"] == "GAMMA0_ELLIPSOID"
    assert data["dataFilter"]["acquisitionMode"] == "IW"
    assert data["dataFilter"]["polarization"] == "DV"
    assert data["dataFilter"]["orbitDirection"] == "DESCENDING"
    assert data["dataFilter"]["timeRange"]["from"] == ACQ
    assert data["dataFilter"]["timeRange"]["to"] == "2024-09-29T00:26:11Z"
    assert req["output"]["responses"][0]["format"]["type"] == "image/tiff"
    assert req["input"]["bounds"]["properties"]["crs"].endswith("/4326")
    assert "toDb" in req["evalscript"]
    assert "VV" in req["evalscript"] and "VH" in req["evalscript"]


def test_build_s2_scl_request():
    req = cdse.build_s2_scl_process_request(BBOX, ACQ, 50, 60)
    assert req["input"]["data"][0]["type"] == "sentinel-2-l2a"
    assert "SCL" in req["evalscript"]


def test_build_s2_scl_request_spans_the_whole_datatake():
    # The STAC datetime is the datatake *start*; a narrow window around it
    # returns an empty (all-nodata) mosaic, so the window must cover the whole
    # datatake (which lasts ~20-25 min). The span is config, not a code constant.
    req = cdse.build_s2_scl_process_request(BBOX, ACQ, 50, 60)
    window = req["input"]["data"][0]["dataFilter"]["timeRange"]
    assert window["from"] == ACQ
    start = dt.datetime.fromisoformat(ACQ.replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
    span = (end - start).total_seconds()
    assert span >= 20 * 60
    assert span == app_config.fetch.s2_datatake_window_seconds
    assert cdse.s2_datatake_window_seconds() == span


def test_s2_datatake_window_is_read_from_config(monkeypatch):
    # fetch.s2_datatake_window_seconds is read at call time, so config changes
    # take effect without touching the code.
    monkeypatch.setattr(app_config.fetch, "s2_datatake_window_seconds", 60.0)
    assert cdse.s2_datatake_window_seconds() == 60.0
    req = cdse.build_s2_scl_process_request(BBOX, ACQ, 50, 60)
    window = req["input"]["data"][0]["dataFilter"]["timeRange"]
    assert window["to"] == "2024-09-29T00:26:11Z"  # ACQ + 60s


def test_s1_evalscript_requests_data_mask():
    assert "dataMask" in cdse.S1_DB_EVALSCRIPT
    assert "bands: 3" in cdse.S1_DB_EVALSCRIPT
    req = cdse.build_s1_process_request(BBOX, ACQ, 100, 100)
    assert req["output"]["width"] == 100  # still the requested grid
    # The mask is a third band on top of VV/VH.
    assert "samples.dataMask" in req["evalscript"]


def test_processing_version_tracks_processing():
    a = cdse.build_s1_process_request(BBOX, ACQ, 100, 100)
    b = cdse.build_s1_process_request(BBOX, ACQ, 100, 100, back_coeff="SIGMA0_ELLIPSOID")
    c = cdse.build_s1_process_request(BBOX, ACQ, 200, 200)
    assert cdse._processing_version(a) == cdse._processing_version(c)  # size lives in the key
    assert cdse._processing_version(a) != cdse._processing_version(b)  # processing is


def test_processing_version_tracks_time_window():
    # Changing the time window changes the pixels, so it must invalidate the cache.
    a = cdse.build_s2_scl_process_request(BBOX, ACQ, 50, 60)
    b = cdse.build_s2_scl_process_request(BBOX, ACQ, 50, 60)
    b["input"]["data"][0]["dataFilter"]["timeRange"] = {
        "from": ACQ,
        "to": "2024-09-29T00:26:11Z",
    }
    assert cdse._processing_version(a) != cdse._processing_version(b)


def test_s1_cache_key_changes_with_output_resolution(tmp_path, monkeypatch):
    # Same scene/bbox at two resolutions must not share a cache file (otherwise a
    # 10 m clip is served for a 20 m request).
    monkeypatch.setattr(cdse, "_post_process", lambda payload, timeout: b"II*\x00x")
    scene = {"scene_id": "s1", "acquisition_time": ACQ, "orbit_direction": "DESCENDING"}
    a = cdse.fetch_s1_clip(BBOX, scene, resolution_m=10.0, cache_root=str(tmp_path))
    b = cdse.fetch_s1_clip(BBOX, scene, resolution_m=20.0, cache_root=str(tmp_path))
    assert a.path != b.path
    assert a.from_cache is False and b.from_cache is False


def test_s2_scl_cache_key_changes_with_output_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(cdse, "_post_process", lambda payload, timeout: b"II*\x00x")
    scene = {"scene_id": "s2", "acquisition_time": ACQ, "cloud_pct": 5.0}
    a = cdse.fetch_s2_scl_clip(BBOX, scene, resolution_m=20.0, cache_root=str(tmp_path))
    b = cdse.fetch_s2_scl_clip(BBOX, scene, resolution_m=40.0, cache_root=str(tmp_path))
    assert a.path != b.path


# --- cloud estimate ---------------------------------------------------------
def test_cloud_pct_from_scl_basic():
    scl = np.array([[8, 9, 4, 6], [10, 4, 6, 6]])
    # 3 cloudy out of 8 valid
    assert cdse.cloud_pct_from_scl(scl) == pytest.approx(37.5)


def test_cloud_pct_from_scl_ignores_nodata_and_shadow():
    scl = np.array([[0, 0, 8, 3]])
    # nodata (0) excluded, shadow (3) not cloud -> 1/2 valid
    assert cdse.cloud_pct_from_scl(scl) == pytest.approx(50.0)


def test_cloud_pct_from_scl_all_nodata_is_none(caplog):
    # A clip with zero valid pixels has undefined cloud cover, never 0.0.
    import logging

    with caplog.at_level(logging.WARNING, logger="dyotak.cdse"):
        assert cdse.cloud_pct_from_scl(np.zeros((3, 3), dtype=int)) is None
    assert any("zero valid pixels" in r.message for r in caplog.records)


def test_estimate_cloud_pct_none_for_all_nodata_clip():
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.zeros((4, 4), dtype="float32"), photometric="minisblack")
    clip = cdse.ClipResult(buf.getvalue(), None, False)
    assert cdse.estimate_cloud_pct(clip) is None


def test_scl_class_histogram_counts_and_valid_fraction():
    scl = np.array([[0, 0, 1, 8], [9, 10, 4, 4]])
    hist = cdse.scl_class_histogram(scl)
    assert hist["total"] == 8
    assert hist["valid"] == 6
    assert hist["valid_pct"] == pytest.approx(75.0)
    assert set(hist["counts"]) == set(range(12))
    assert hist["counts"][0] == 2
    assert hist["counts"][4] == 2
    assert hist["counts"][8] == 1
    assert hist["counts"][11] == 0


def test_scl_class_histogram_all_nodata():
    hist = cdse.scl_class_histogram(np.zeros((2, 2), dtype=int))
    assert hist["valid"] == 0
    assert hist["valid_pct"] == 0.0
    assert hist["counts"][0] == 4


# --- caching -----------------------------------------------------------------
def test_fetch_s1_clip_caches_on_disk(tmp_path, monkeypatch):
    calls = {"n": 0}
    fake_tiff = b"II*\x00" + b"fake-tiff-payload"

    def fake_post(payload, timeout):
        calls["n"] += 1
        return fake_tiff

    monkeypatch.setattr(cdse, "_post_process", fake_post)

    scene = {"scene_id": "s1", "acquisition_time": ACQ, "orbit_direction": "DESCENDING"}
    first = cdse.fetch_s1_clip(BBOX, scene, cache_root=str(tmp_path))
    assert first.from_cache is False
    assert first.data == fake_tiff
    assert first.path.is_file()

    second = cdse.fetch_s1_clip(BBOX, scene, cache_root=str(tmp_path))
    assert second.from_cache is True
    assert second.data == fake_tiff
    assert calls["n"] == 1  # network hit exactly once


def test_fetch_s1_clip_reports_progress_instead_of_printing(tmp_path, monkeypatch, capsys):
    # The old print() is gone: progress goes to the caller (and the logger).
    monkeypatch.setattr(cdse, "_post_process", lambda payload, timeout: b"II*\x00x")
    messages = []
    scene = {"scene_id": "s1-scene", "acquisition_time": ACQ, "orbit_direction": "DESCENDING"}
    first = cdse.fetch_s1_clip(BBOX, scene, cache_root=str(tmp_path), progress=messages.append)
    assert first.from_cache is False
    assert len(messages) == 1
    assert "Downloading S1 radar clip" in messages[0]
    assert "s1-scene" in messages[0]
    assert capsys.readouterr().out == ""  # nothing printed to stdout

    # The cached re-fetch downloads nothing, so it reports nothing.
    cdse.fetch_s1_clip(BBOX, scene, cache_root=str(tmp_path), progress=messages.append)
    assert len(messages) == 1


def test_cache_key_changes_with_scene_time(tmp_path, monkeypatch):
    monkeypatch.setattr(cdse, "_post_process", lambda payload, timeout: b"II*\x00x")
    s1 = {"scene_id": "a", "acquisition_time": "2024-09-29T00:25:11Z", "orbit_direction": "DESCENDING"}
    s2 = {"scene_id": "b", "acquisition_time": "2024-09-17T00:25:11Z", "orbit_direction": "DESCENDING"}
    a = cdse.fetch_s1_clip(BBOX, s1, cache_root=str(tmp_path))
    b = cdse.fetch_s1_clip(BBOX, s2, cache_root=str(tmp_path))
    assert a.path != b.path


# --- auth --------------------------------------------------------------------
def test_get_access_token_without_credentials(monkeypatch):
    monkeypatch.setattr(cdse, "_credentials", lambda: (None, None))
    with pytest.raises(CdseUnavailableError):
        cdse.get_access_token()


def test_stac_search_auth_fails_fast(monkeypatch):
    calls = {"n": 0}

    class Resp:
        status_code = 401
        text = "unauthorized"

        def json(self):
            return {}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(cdse.requests, "post", fake_post)
    with pytest.raises(CdseAuthError):
        cdse.search_sentinel1_scenes(BBOX, "2024-09-01", "2024-09-30")
    assert calls["n"] == 1  # auth error is not retried


def test_token_auth_fails_fast(monkeypatch):
    cdse.reset_token_cache()
    calls = {"n": 0}

    class Resp:
        status_code = 401
        text = "invalid_client"

        def json(self):
            return {}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(cdse, "_credentials", lambda: ("id", "secret"))
    monkeypatch.setattr(cdse.requests, "post", fake_post)
    with pytest.raises(CdseAuthError):
        cdse.get_access_token(force_refresh=True)
    assert calls["n"] == 1


def test_missing_credentials_is_auth_error(monkeypatch):
    monkeypatch.setattr(cdse, "_credentials", lambda: (None, None))
    with pytest.raises(CdseAuthError):
        cdse.get_access_token(force_refresh=True)


def test_search_sentinel1_scenes_normalizes_and_sorts(monkeypatch):
    def fake_stac(body, timeout):
        older = dict(S1_FEATURE)
        older["id"] = "old"
        older["properties"] = dict(S1_FEATURE["properties"])
        older["properties"]["datetime"] = "2024-09-17T00:25:11Z"
        return {"type": "FeatureCollection", "features": [S1_FEATURE, older]}

    monkeypatch.setattr(cdse, "_stac_search", fake_stac)
    scenes = cdse.search_sentinel1_scenes(BBOX, "2024-09-01", "2024-09-30")
    assert [s["scene_id"] for s in scenes] == ["old", S1_FEATURE["id"]]
