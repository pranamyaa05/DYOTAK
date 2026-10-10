"""Unit tests for app.pipeline.ohsome (no network)."""

import pytest

from app.common.errors import (
    NonRetryableError,
    OhsomeAuthError,
    OhsomeRequestError,
    OhsomeUnavailableError,
)
from app.pipeline import ohsome
from app.settings import app_config

BBOX = [85.15, 27.85, 85.35, 28.05]


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, content=b"", text=None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", "replace")


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
    assert body["clip"] is True


def test_build_v2_request_uses_timezone_aware_datetimes():
    """ohsome v2 rejects bare dates; time.start/time.end must be ISO-8601 UTC."""
    body = ohsome.build_v2_request(BBOX, "2024-09-25", "building=*")
    assert body["time"] == {
        "start": "2024-09-25T00:00:00Z",
        "end": "2024-09-25T00:00:00Z",
    }
    widened = ohsome.build_v2_request(BBOX, "2024-09-25", "building=*", end_date="2024-09-26")
    assert widened["time"] == {
        "start": "2024-09-25T00:00:00Z",
        "end": "2024-09-26T00:00:00Z",
    }


def test_iso_utc_and_plus_days():
    assert ohsome._iso_utc("2024-09-25") == "2024-09-25T00:00:00Z"
    assert ohsome._iso_utc("2024-09-25T06:00:00Z") == "2024-09-25T06:00:00Z"
    assert ohsome._plus_days("2024-09-25", 1) == "2024-09-26T00:00:00Z"


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


def test_fetch_features_parquet_falls_back_to_day_range_on_422(monkeypatch):
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "test-key")
    bodies = []
    responses = [
        _Resp(422, text='{"detail":"End timestamp needs to be greater than start timestamp."}'),
        _Resp(200, content=b"PAR1parquet"),
    ]

    def fake_post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        return responses[len(bodies) - 1]

    monkeypatch.setattr(ohsome.requests, "post", fake_post)
    extract = ohsome.fetch_features_parquet(
        BBOX, "2024-09-25", "building=*", event_date="2024-09-28"
    )
    assert extract.data == b"PAR1parquet"
    assert extract.time_form == ohsome.TIME_FORM_DAY
    assert len(bodies) == 2
    # 1st attempt: point window, timezone-aware datetimes.
    assert bodies[0]["time"] == {
        "start": "2024-09-25T00:00:00Z",
        "end": "2024-09-25T00:00:00Z",
    }
    # 2nd attempt: end = start + 1 day, still before the event date.
    assert bodies[1]["time"] == {
        "start": "2024-09-25T00:00:00Z",
        "end": "2024-09-26T00:00:00Z",
    }


def test_fetch_features_parquet_422_is_not_retried(monkeypatch):
    """A 422 must not be retried. With event_date == snapshot+1 the one-day
    fallback is not allowed, so exactly one request may be attempted."""
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "test-key")
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(422, text='{"detail":"Input should be a valid datetime"}')

    monkeypatch.setattr(ohsome.requests, "post", fake_post)
    with pytest.raises(OhsomeRequestError) as excinfo:
        ohsome.fetch_features_parquet(
            BBOX, "2024-09-25", "building=*", event_date="2024-09-26"
        )
    assert isinstance(excinfo.value, NonRetryableError)
    assert calls["n"] == 1  # no retry (retries would have produced 3 calls)


def test_fetch_features_parquet_reports_both_bodies_when_both_fail(monkeypatch):
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "test-key")
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(422, text=f"body-{calls['n']}")

    monkeypatch.setattr(ohsome.requests, "post", fake_post)
    with pytest.raises(OhsomeRequestError) as excinfo:
        ohsome.fetch_features_parquet(
            BBOX, "2024-09-25", "building=*", event_date="2024-09-28"
        )
    message = str(excinfo.value)
    assert "body-1" in message and "body-2" in message
    assert calls["n"] == 2


def test_fetch_features_parquet_retries_on_5xx_without_nesting(monkeypatch):
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "test-key")
    monkeypatch.setattr("app.common.retry.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(500, text="boom")

    monkeypatch.setattr(ohsome.requests, "post", fake_post)
    with pytest.raises(OhsomeUnavailableError) as excinfo:
        ohsome.fetch_features_parquet(
            BBOX, "2024-09-25", "building=*", event_date="2024-09-28"
        )
    assert calls["n"] == app_config.osm.retries  # 5xx is retried
    # The message must not be wrapped inside itself.
    assert str(excinfo.value).count("ohsome v2 returned 500") == 1


def test_fetch_features_parquet_401_is_not_retried(monkeypatch):
    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "bad-key")
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(401, text="unauthorized")

    monkeypatch.setattr(ohsome.requests, "post", fake_post)
    with pytest.raises(OhsomeAuthError):
        ohsome.fetch_features_parquet(BBOX, "2024-09-25", "building=*")
    assert calls["n"] == 1


def test_fetch_v2_caches_parquet(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_parquet(bbox, snapshot_date, ohsome_filter, api_key=None, timeout=None, event_date=None):
        calls["n"] += 1
        return ohsome.OhsomeExtract(
            b"PAR1fake-parquet",
            ohsome.TIME_FORM_DAY,
            "2024-09-25T00:00:00Z",
            "2024-09-26T00:00:00Z",
        )

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
    assert first["time_window"]["form"] == ohsome.TIME_FORM_DAY

    second = ohsome.fetch_preevent_osm_elements(
        BBOX, "2024-09-25", "2024-09-28", feature_types=["building"],
        backend="v2", cache_root=str(tmp_path),
    )
    assert second["from_cache"] is True
    assert calls["n"] == 1
    # The time form is persisted, so a cache hit still reports it.
    assert second["time_window"] == first["time_window"]


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


def test_fetch_ohsome_metadata_reports_latest_snapshot(monkeypatch):
    calls = {"url": None, "headers": None}

    class _Meta:
        status_code = 200

        @staticmethod
        def json():
            return {
                "apiVersion": "2.0.0",
                "temporalExtent": {
                    "start": "2007-10-08T00:00:00Z",
                    "end": "2026-10-09T07:25:28Z",
                },
            }

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        return _Meta()

    monkeypatch.setattr(ohsome.settings, "ohsome_api_key", "secret-key")
    monkeypatch.setattr(ohsome.requests, "get", fake_get)

    meta = ohsome.fetch_ohsome_metadata()
    assert calls["url"] == f"{app_config.osm.v2_base_url.rstrip('/')}/metadata"
    assert calls["headers"] == {"Authorization": "secret-key"}
    assert ohsome.latest_osm_snapshot(meta) == "2026-10-09T07:25:28Z"


def test_fetch_ohsome_metadata_classifies_errors(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _Resp(503, b"upstream down")

    monkeypatch.setattr(ohsome.requests, "get", fake_get)
    with pytest.raises(OhsomeUnavailableError) as excinfo:
        ohsome.fetch_ohsome_metadata()
    assert "upstream down" in str(excinfo.value) or "upstream down" in repr(excinfo.value)

    monkeypatch.setattr(ohsome.requests, "get", lambda *a, **k: _Resp(403, b"nope"))
    with pytest.raises(OhsomeAuthError):
        ohsome.fetch_ohsome_metadata()


def test_max_edit_timestamp_reads_parquet():
    import datetime as dt
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    def _micros(year, month, day, hour, minute, second):
        moment = dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)
        return int(moment.timestamp() * 1_000_000)

    table = pa.table(
        {
            "edit_timestamp": pa.array(
                [_micros(2026, 3, 9, 23, 59, 23), _micros(2015, 4, 26, 21, 11, 29)],
                type=pa.timestamp("us", tz="UTC"),
            )
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    assert ohsome.max_edit_timestamp(buf.getvalue()) == "2026-03-09T23:59:23Z"
    # Undecodable input yields None rather than raising.
    assert ohsome.max_edit_timestamp(b"not parquet") is None


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
