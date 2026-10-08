"""Unit tests for app.pipeline.dem (no network)."""

import pytest

from app.pipeline import dem

BBOX = [85.15, 27.85, 85.35, 28.05]


def test_dem_tile_id_trishuli():
    assert dem.dem_tile_id(27.9, 85.2) == "Copernicus_DSM_COG_10_N27_00_E085_00_DEM"


def test_dem_tile_id_southern_western_and_origin():
    assert dem.dem_tile_id(-1.2, -178.5) == "Copernicus_DSM_COG_10_S02_00_W179_00_DEM"
    assert dem.dem_tile_id(0.0, 0.0) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"


def test_dem_tiles_for_bbox_crosses_two_lat_bands():
    tiles = dem.dem_tiles_for_bbox(BBOX)
    assert tiles == [
        "Copernicus_DSM_COG_10_N27_00_E085_00_DEM",
        "Copernicus_DSM_COG_10_N28_00_E085_00_DEM",
    ]


def test_dem_tile_url():
    tid = "Copernicus_DSM_COG_10_N27_00_E085_00_DEM"
    assert dem.dem_tile_url(tid) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        f"{tid}/{tid}.tif"
    )


def test_is_tiff():
    assert dem.is_tiff(b"II*\x00rest")
    assert dem.is_tiff(b"MM\x00*rest")
    assert not dem.is_tiff(b"PK\x03\x04zip")


def test_parse_tile_id():
    assert dem.parse_tile_id("Copernicus_DSM_COG_10_N27_00_E085_00_DEM") == (27, 85)
    assert dem.parse_tile_id("Copernicus_DSM_COG_10_S02_00_W179_00_DEM") == (-2, -179)


def test_tile_window_for_subbbox_within_one_tile():
    # bbox entirely inside tile N27; top-left origin is (lon 85, lat 28).
    tid = "Copernicus_DSM_COG_10_N27_00_E085_00_DEM"
    row, col, h, w = dem.tile_window_for_subbbox(tid, [85.15, 27.85, 85.35, 28.0])
    assert col == round(0.15 * 3600)      # 540
    assert row == round((28.0 - 28.0) * 3600)  # 0
    assert w == round(0.20 * 3600)        # 720
    assert h == round(0.15 * 3600)        # 540


def test_tile_window_clamps_to_tile_bounds():
    tid = "Copernicus_DSM_COG_10_N27_00_E085_00_DEM"
    row, col, h, w = dem.tile_window_for_subbbox(tid, [84.0, 26.0, 87.0, 30.0])
    assert row == 0 and col == 0
    assert h <= dem.DEM_TILE_PIXELS and w <= dem.DEM_TILE_PIXELS


def test_read_dem_window_requires_rasterio(monkeypatch):
    def boom():
        raise dem.DemUnavailableError(details="rasterio missing")

    monkeypatch.setattr(dem, "_rasterio", boom)
    with pytest.raises(dem.DemUnavailableError):
        dem.read_dem_window(BBOX)


def test_fetch_dem_tile_caches(tmp_path, monkeypatch):
    fake_tiff = b"II*\x00" + b"dem-tile"
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        content = fake_tiff

    def fake_get(url, timeout):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(dem.requests, "get", fake_get)

    tid = "Copernicus_DSM_COG_10_N27_00_E085_00_DEM"
    p1 = dem.fetch_dem_tile(tid, cache_root=str(tmp_path))
    p2 = dem.fetch_dem_tile(tid, cache_root=str(tmp_path))
    assert p1 == p2
    assert calls["n"] == 1  # cached after the first fetch
    assert dem.is_tiff(open(p1, "rb").read())
