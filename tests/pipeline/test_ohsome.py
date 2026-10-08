"""Unit tests for app.pipeline.ohsome (no network)."""

import pytest

import pytest

from app.common.errors import OhsomeAuthError, OhsomeUnavailableError
from app.pipeline import ohsome
from app.settings import app_config

BBOX = [85.15, 27.85, 85.35, 28.05]


def test_config_feature_filters_are_known():
    for feature in app_config.osm.feature_filters:
        assert feature in ohsome.FEATURE_FILTERS, f"no ohsome filter for {feature}"


def test_build_filter_known_types():
    f = ohsome.build_filter(["building", "bridge"])
    assert "building=*" in f
    assert "bridge=*" in f
    assert " or " in f


def test_build_filter_rejects_unknown():
    with pytest.raises(ValueError):
        ohsome.build_filter(["spaceships"])


def test_build_v2_request_shape():
    body = ohsome.build_v2_request(BBOX, "2024-09-25", "building=*")
    assert body["aoi"] == BBOX
    assert body["filter"] == "building=*"
    assert body["time"] == {"start": "2024-09-25", "end": "2024-09-25"}
    assert body["clip"] is True


def test_fetch_features_parquet_requires_key(monkeypatch):
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", None)
    with pytest.raises(OhsomeAuthError):
        ohsome.fetch_features_parquet(BBOX, "2024-09-25", "building=*")


def test_overpass_timestamp_normalisation():
    assert ohsome._overpass_timestamp("2024-09-25") == "2024-09-25T00:00:00Z"
    assert ohsome._overpass_timestamp("2024-09-25T06:00:00Z") == "2024-09-25T06:00:00Z"


def test_build_overpass_attic_query():
    q = ohsome.build_overpass_attic_query(BBOX, "2024-09-25", ["building", "bridge"])
    assert '[date:"2024-09-25T00:00:00Z"]' in q
    # Overpass bbox order is (south, west, north, east)
    assert 'way["building"](27.85,85.15,28.05,85.35)' in q
    assert 'way["bridge"]' in q
    assert q.rstrip().endswith("out geom;")


def test_build_overpass_query_rejects_unknown():
    with pytest.raises(ValueError):
        ohsome.build_overpass_attic_query(BBOX, "2024-09-25", ["spaceships"])


def test_fetch_v2_caches_parquet(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_parquet(bbox, snapshot_date, ohsome_filter, api_key=None, timeout=None):
        calls["n"] += 1
        return b"PAR1fake-parquet"

    monkeypatch.setattr(ohsome, "fetch_features_parquet", fake_parquet)
    monkeypatch.setattr(ohsome, "count_parquet_features", lambda data: 5)

    first = ohsome.fetch_preevent_osm_elements(
        BBOX, "2024-09-25", "2024-09-28", feature_types=["building"],
        backend="v2", cache_root=str(tmp_path),
    )
    assert first["format"] == "geoparquet"
    assert first["counts"] == {"building": 5}
    assert first["total"] == 5
    assert first["from_cache"] is False

    second = ohsome.fetch_preevent_osm_elements(
        BBOX, "2024-09-25", "2024-09-28", feature_types=["building"],
        backend="v2", cache_root=str(tmp_path),
    )
    assert second["from_cache"] is True
    assert calls["n"] == 1


def test_validate_snapshot_rejects_on_or_after_event():
    with pytest.raises(AssertionError):
        ohsome.validate_osm_snapshot_date("2024-09-28", "2024-09-28")
    with pytest.raises(AssertionError):
        ohsome.validate_osm_snapshot_date("2024-10-01", "2024-09-28")
    # strictly before is accepted
    ohsome.validate_osm_snapshot_date("2024-09-25", "2024-09-28")


def test_fetch_preevent_enforces_snapshot_order():
    with pytest.raises(AssertionError):
        ohsome.fetch_preevent_osm_elements(BBOX, "2024-09-28", "2024-09-28")


def test_fetch_preevent_merges_and_caches(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_collection(bbox, snapshot_date, ohsome_filter, timeout=None):
        calls["n"] += 1
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": None, "properties": {"id": calls["n"]}},
                {"type": "Feature", "geometry": None, "properties": {"id": calls["n"]}},
            ],
        }

    monkeypatch.setattr(ohsome, "fetch_feature_collection", fake_collection)

    first = ohsome.fetch_preevent_osm_elements(
        BBOX, "2024-09-25", "2024-09-28", feature_types=["building", "highway"],
        backend="v1", cache_root=str(tmp_path),
    )
    assert first["from_cache"] is False
    assert first["counts"] == {"building": 2, "highway": 2}
    assert len(first["features"]) == 4
    assert {f["properties"]["_dyotak_feature_type"] for f in first["features"]} == {"building", "highway"}
    assert calls["n"] == 2  # one request per feature type

    second = ohsome.fetch_preevent_osm_elements(
        BBOX, "2024-09-25", "2024-09-28", feature_types=["building", "highway"],
        backend="v1", cache_root=str(tmp_path),
    )
    assert second["from_cache"] is True
    assert calls["n"] == 2  # cache hit, no new network calls
