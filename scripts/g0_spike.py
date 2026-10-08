#!/usr/bin/env python
"""G0 data-access spike (PLAN.md G0 gate).

Runs every external data path for an AOI and prints a PASS/FAIL/SKIP table:

  1. preset + credentials
  2. CDSE OAuth token
  3. Sentinel-1 GRD IW catalog search (relative orbit, direction, time, id)
  4. same-orbit/same-direction pairing rule
  5. Sentinel-1 VV/VH clip (orthorectified, Copernicus DEM, dB)
  6. Sentinel-2 L2A catalog search
  7. Sentinel-2 SCL clip + cloud estimate
  8. ohsome pre-event OSM extraction (buildings, roads, bridges, health, places)
  9. Copernicus DEM 30m tiles (AWS)
 10. disk cache round-trip

Usage:
    python scripts/g0_spike.py                       # Trishuli preset
    python scripts/g0_spike.py --preset other_aoi
    python scripts/g0_spike.py --skip-clips          # catalog/ohsome/DEM only

Exit code is non-zero if any check FAILs. SKIP (e.g. missing credentials) does
not fail the run — it is reported honestly.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Make the repo root importable when run as `python scripts/g0_spike.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

from app.common.errors import DyotakError  # noqa: E402
from app.pipeline import cdse, dem, ohsome  # noqa: E402
from app.pipeline.pairing import validate_and_pair_s1_scenes  # noqa: E402
from app.settings import app_config, settings  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    seconds: float


class Runner:
    def __init__(self) -> None:
        self.checks: List[Check] = []

    def run(self, name: str, fn: Callable[[], Tuple[str, str]]) -> Tuple[str, str]:
        start = time.time()
        try:
            status, detail = fn()
        except (DyotakError, AssertionError, ValueError) as exc:
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - spike must never crash the table
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        self.checks.append(Check(name, status, detail, time.time() - start))
        marker = {"PASS": "\033[92mPASS\033[0m", "FAIL": "\033[91mFAIL\033[0m", "SKIP": "\033[93mSKIP\033[0m"}[status]
        print(f"  [{marker}] {name}: {detail} ({time.time() - start:.1f}s)", flush=True)
        return status, detail

    def render(self) -> int:
        width = max((len(c.name) for c in self.checks), default=10)
        print("\n" + "=" * 78)
        print(f"{'CHECK'.ljust(width)}  STATUS  DETAIL")
        print("-" * 78)
        for c in self.checks:
            print(f"{c.name.ljust(width)}  {c.status:<6}  {c.detail}")
        print("=" * 78)

        failures = sum(1 for c in self.checks if c.status == FAIL)
        skipped = sum(1 for c in self.checks if c.status == SKIP)
        passed = sum(1 for c in self.checks if c.status == PASS)
        print(f"summary: {passed} passed, {failures} failed, {skipped} skipped")
        return 1 if failures else 0


def load_preset(preset_id: str) -> dict:
    path = REPO_ROOT / "config" / "presets" / f"{preset_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"preset not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def s1_pairing_report(
    scenes: List[dict],
    event_date: str,
    repeat_days: int,
    max_pre_scenes: int = 3,
    tolerance: int = 2,
) -> dict:
    """Pure: select the expected post/pre scenes and verify the repeat gap.

    Checks (raises ValueError with a clear reason on failure):
      - a post-event scene exists and is the FIRST acquisition on/after the event
      - a same-orbit/same-direction pre scene exists (reuses the pipeline rule)
      - the pre/post gap matches the Sentinel-1 repeat cycle within tolerance

    Returns a report dict including the raw post/pre scene dicts.
    """
    if not scenes:
        raise ValueError("no Sentinel-1 scenes in the search window")
    ordered = sorted(scenes, key=lambda s: s["acquisition_time"])
    posts = [s for s in ordered if s["acquisition_time"][:10] >= event_date]
    pres = [s for s in ordered if s["acquisition_time"][:10] < event_date]
    if not posts:
        raise ValueError(f"no post-event scene on/after {event_date}")

    post = posts[0]
    earliest_post = min(s["acquisition_time"] for s in posts)
    if post["acquisition_time"] != earliest_post:
        raise ValueError("post scene is not the first acquisition on/after the event")

    pair = validate_and_pair_s1_scenes(post, pres, max_pre_scenes=max_pre_scenes)
    pre = pair.pre[0]
    post_date = date.fromisoformat(post["acquisition_time"][:10])
    pre_date = date.fromisoformat(pre.acquisition_time[:10])
    gap_days = (post_date - pre_date).days
    if gap_days <= 0:
        raise ValueError(f"pre scene {pre_date} is not before post scene {post_date}")
    if not (repeat_days - tolerance <= gap_days <= repeat_days + tolerance):
        raise ValueError(
            f"pre/post gap {gap_days}d outside expected repeat cycle "
            f"{repeat_days}\u00b1{tolerance}d"
        )

    return {
        "post_date": post_date.isoformat(),
        "pre_date": pre_date.isoformat(),
        "gap_days": gap_days,
        "expected_days": repeat_days,
        "orbit": pair.relative_orbit,
        "direction": pair.direction.value,
        "pre_count": len(pair.pre),
        "post_scene": post,
        "pre_scene": pre.model_dump(),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DYOTAK G0 data-access spike")
    parser.add_argument("--preset", default="trishuli_emsr927")
    parser.add_argument("--skip-clips", action="store_true", help="skip Process API clips")
    args = parser.parse_args(argv)

    runner = Runner()
    print(f"DYOTAK G0 spike — preset={args.preset}\n")

    # 1. preset
    preset: dict = {}
    def _preset():
        nonlocal preset
        preset = load_preset(args.preset)
        return PASS, f"bbox={preset['bbox']} event={preset['event_date']} osm={preset.get('osm_snapshot_date')}"
    runner.run("preset", _preset)

    if not preset:
        return runner.render()

    bbox = [float(c) for c in preset["bbox"]]
    event_date = str(preset["event_date"])
    osm_snapshot = str(preset.get("osm_snapshot_date") or (date.fromisoformat(event_date) - timedelta(days=3)).isoformat())

    creds = bool(settings.cdse_client_id and settings.cdse_client_secret)

    def _creds():
        if creds:
            return PASS, "DYOTAK_CDSE_CLIENT_ID / _SECRET present"
        return SKIP, "CDSE credentials not set; token/clip checks will skip"
    runner.run("credentials", _creds)

    def _token():
        if not creds:
            return SKIP, "no credentials"
        token = cdse.get_access_token()
        return PASS, f"token acquired (len={len(token)})"
    runner.run("cdse oauth token", _token)

    # 3. S1 catalog
    search_start = (date.fromisoformat(event_date) - timedelta(days=app_config.pairing.search_window_days)).isoformat()
    search_end = (date.fromisoformat(event_date) + timedelta(days=7)).isoformat()
    s1_scenes: List[dict] = []

    def _s1_catalog():
        nonlocal s1_scenes
        s1_scenes = cdse.search_sentinel1_scenes(bbox, search_start, search_end, instrument_mode="IW")
        if not s1_scenes:
            return FAIL, f"no S1 GRD IW scenes in {search_start}..{search_end}"
        s = s1_scenes[-1]
        return PASS, (
            f"{len(s1_scenes)} scenes; latest dir={s['orbit_direction']} "
            f"orbit={s['relative_orbit']} t={s['acquisition_time']} id={s['scene_id'][:32]}"
        )
    runner.run("s1 catalog search", _s1_catalog)

    # 4. pairing + repeat-gap assertions
    post_scene: Optional[dict] = None

    def _pairing():
        nonlocal post_scene
        if not s1_scenes:
            return SKIP, "no catalog results"
        try:
            report = s1_pairing_report(
                s1_scenes,
                event_date,
                app_config.pairing.primary_repeat_days,
                max_pre_scenes=app_config.pairing.max_pre_scenes,
            )
        except (ValueError, AssertionError) as exc:
            return FAIL, f"pairing check failed: {exc}"
        post_scene = report["post_scene"]
        return PASS, (
            f"post={report['post_date']} (first on/after {event_date}) "
            f"pre={report['pre_date']} gap={report['gap_days']}d "
            f"(expected {report['expected_days']}d) orbit={report['orbit']} "
            f"dir={report['direction']} pre_scenes={report['pre_count']}"
        )
    runner.run("s1 pairing rule (repeat gap)", _pairing)

    # 5. S1 clip
    def _s1_clip():
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if post_scene is None:
            return SKIP, "no post scene"
        clip = cdse.fetch_s1_clip(bbox, post_scene)
        if clip.data[:4] not in (b"II*\x00", b"MM\x00*"):
            return FAIL, "clip is not a GeoTIFF"
        try:
            import io

            import tifffile

            shape = tuple(tifffile.imread(io.BytesIO(clip.data)).shape)
        except Exception as exc:  # noqa: BLE001
            shape = f"decode-failed({type(exc).__name__})"
        return PASS, (
            f"{len(clip.data)} bytes {clip.width}x{clip.height} tiff_shape={shape} "
            f"cached={clip.from_cache}"
        )
    runner.run("s1 process clip (VV/VH dB)", _s1_clip)

    # 6. S2 catalog
    s2_scenes: List[dict] = []
    def _s2_catalog():
        nonlocal s2_scenes
        s2_scenes = cdse.search_sentinel2_scenes(bbox, search_start, search_end, max_cloud=100)
        if not s2_scenes:
            return FAIL, "no S2 L2A scenes in window"
        best = min(s2_scenes, key=lambda s: (s.get("cloud_pct") if s.get("cloud_pct") is not None else 100))
        return PASS, f"{len(s2_scenes)} scenes; clearest cloud={best.get('cloud_pct')} t={best['acquisition_time']}"
    runner.run("s2 catalog search", _s2_catalog)

    # 7. S2 clip + cloud estimate
    s2_best: Optional[dict] = None
    def _s2_cloud():
        nonlocal s2_best
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if not s2_scenes:
            return SKIP, "no S2 scenes"
        on_after = [s for s in s2_scenes if (s.get("acquisition_time") or "")[:10] >= event_date]
        pool = on_after or s2_scenes
        s2_best = min(pool, key=lambda s: (s.get("cloud_pct") if s.get("cloud_pct") is not None else 100))
        clip = cdse.fetch_s2_scl_clip(bbox, s2_best, resolution_m=app_config.fetch.resolution_m * 2)
        pct = cdse.estimate_cloud_pct(clip)
        return PASS, f"SCL clip {len(clip.data)} bytes cloud={pct}% (catalog={s2_best.get('cloud_pct')})"
    runner.run("s2 scl clip + cloud estimate", _s2_cloud)

    # 8. ohsome
    ohsome_state = {"ran": False, "backend": app_config.osm.backend}

    def _ohsome():
        backend = app_config.osm.backend
        if backend == "v2" and not settings.ohsome_api_key:
            return SKIP, "ohsome v2 needs DYOTAK_OHSOME_API_KEY (v1 geometry answers 403)"
        fc = ohsome.fetch_preevent_osm_elements(
            bbox, osm_snapshot, event_date, backend=backend
        )
        ohsome_state["ran"] = True
        counts = fc.get("counts", {})
        total = fc.get("total", sum(counts.values()))
        detail = f"snapshot={osm_snapshot} format={fc.get('format')} counts={counts}"
        if total <= 0:
            return FAIL, detail
        return PASS, detail
    runner.run("ohsome pre-event osm", _ohsome)

    # 9. DEM (rasterio windowed HTTP range reads)
    dem_state = {"ran": False}

    def _dem():
        window = dem.read_dem_window(bbox)
        arr = np.asarray(window.array)
        if not np.isfinite(arr).any():
            return FAIL, "DEM window contains no valid pixels"
        dem_state["ran"] = True
        tiles = dem.dem_tiles_for_bbox(bbox)
        lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
        return PASS, (
            f"{window.shape[0]}x{window.shape[1]}px tiles={len(tiles)} "
            f"elev {lo:.0f}..{hi:.0f} m cached={window.from_cache} (range read)"
        )
    runner.run("copernicus dem 30m (range)", _dem)

    # 10. cache round-trip
    def _cache():
        wanted = []
        if not args.skip_clips and creds and post_scene is not None:
            wanted.append("s1 clip")
        if ohsome_state["ran"]:
            wanted.append("ohsome")
        if dem_state["ran"]:
            wanted.append("dem window")
        if not wanted:
            return SKIP, "nothing was fetched (no credentials / --skip-clips)"
        served = []
        if "s1 clip" in wanted:
            c = cdse.fetch_s1_clip(bbox, post_scene)
            if c.from_cache:
                served.append("s1 clip")
        if "dem window" in wanted:
            w = dem.read_dem_window(bbox)
            if w.from_cache:
                served.append("dem window")
        if "ohsome" in wanted:
            fc = ohsome.fetch_preevent_osm_elements(
                bbox, osm_snapshot, event_date, backend=ohsome_state["backend"]
            )
            if fc.get("from_cache"):
                served.append("ohsome")
        if len(served) == len(wanted):
            return PASS, "cache hit on re-fetch: " + ", ".join(served)
        return FAIL, f"expected cache hits for {wanted}, got {served}"
    runner.run("disk cache round-trip", _cache)

    return runner.render()


if __name__ == "__main__":
    raise SystemExit(main())
