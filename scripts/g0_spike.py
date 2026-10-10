#!/usr/bin/env python
"""G0 data-access spike (PLAN.md G0 gate).

Runs every external data path for an AOI and prints a PASS/FAIL/WARN/SKIP table:

  1. preset + credentials
  2. CDSE OAuth token
  3. Sentinel-1 GRD IW catalog search (relative orbit, direction, time, id)
  4. same-orbit/same-direction pairing rule
  5. Sentinel-1 VV/VH clip: cold fetch (cache cleared) + cached re-fetch
  6. Sentinel-1 pre-event clip (pairing rule): grid equality + post/pre dB log-ratio
  7. Sentinel-2 L2A catalog search (selection rule shared with the clip)
  8. Sentinel-2 SCL clip: class histogram, valid fraction, cloud estimate
  9. ohsome pre-event OSM extraction (buildings, roads, bridges, health, places)
 10. Copernicus DEM 30m tiles (AWS)
 11. disk cache round-trip

Usage:
    python scripts/g0_spike.py                       # Trishuli preset
    python scripts/g0_spike.py --preset other_aoi
    python scripts/g0_spike.py --skip-clips          # catalog/ohsome/DEM only

Exit code is non-zero if any check FAILs. WARN (e.g. an all-nodata S2 clip or a
Sentinel-1 clip above the nodata budget) and SKIP (e.g. missing credentials) do
not fail the run — they are reported honestly.
"""

from __future__ import annotations

import argparse
import math
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

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


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
        marker = {
            "PASS": "\033[92mPASS\033[0m",
            "FAIL": "\033[91mFAIL\033[0m",
            "WARN": "\033[93mWARN\033[0m",
            "SKIP": "\033[96mSKIP\033[0m",
        }[status]
        print(f"  [{marker}] {name}: {detail} ({time.time() - start:.1f}s)", flush=True)
        return status, detail

    def render(self) -> int:
        width = max((len(c.name) for c in self.checks), default=10)
        print("\n" + "=" * 100)
        print(f"{'CHECK'.ljust(width)}  {'STATUS':<6}  {'SECONDS':>8}  DETAIL")
        print("-" * 100)
        for c in self.checks:
            print(f"{c.name.ljust(width)}  {c.status:<6}  {c.seconds:8.1f}  {c.detail}")
        print("=" * 100)
        total_s = sum(c.seconds for c in self.checks)
        print(f"total elapsed: {total_s:.1f}s")

        failures = sum(1 for c in self.checks if c.status == FAIL)
        warnings = sum(1 for c in self.checks if c.status == WARN)
        skipped = sum(1 for c in self.checks if c.status == SKIP)
        passed = sum(1 for c in self.checks if c.status == PASS)
        print(
            f"summary: {passed} passed, {warnings} warned, "
            f"{failures} failed, {skipped} skipped"
        )
        return 1 if failures else 0


#: Backscatter beyond this (dB) is not plausible surface water/land and is
#: reported as a percentage so a badly scaled clip is obvious.
S1_DB_MAX = 10.0
#: Row is WARN (not PASS) above these shares of bad pixels.
S1_NODATA_WARN_PCT = 5.0
S1_NONFINITE_WARN_PCT = 1.0
#: dB drop below which a pixel counts as a strong backscatter decrease.
S1_DB_DROP = -3.0
#: Same orbit, same unit: the post/pre log-ratio median should sit near 0 dB.
#: A larger offset means the two clips are not radiometrically comparable.
S1_LOG_RATIO_MEDIAN_TOLERANCE_DB = 1.0


def _split_s1_bands(arr) -> Tuple[List, Optional[np.ndarray]]:
    """Split a decoded Sentinel-1 clip into (value bands, dataMask).

    The evalscript returns [VV, VH, dataMask]; older cached clips may still
    return only [VV, VH]. tifffile may hand back (H, W, B) or (B, H, W).
    """
    if arr.ndim == 2:
        return [arr], None
    if arr.ndim == 3 and arr.shape[-1] in (2, 3):
        bands = [arr[..., 0], arr[..., 1]]
        return bands, (arr[..., 2] if arr.shape[-1] == 3 else None)
    if arr.ndim == 3 and arr.shape[0] in (2, 3):
        bands = [arr[0], arr[1]]
        return bands, (arr[2] if arr.shape[0] == 3 else None)
    return [arr], None


def _decode_s1(data: bytes) -> Tuple[List, Optional[np.ndarray]]:
    """Decode a Sentinel-1 clip into (value bands, dataMask)."""
    import io

    import tifffile

    return _split_s1_bands(np.asarray(tifffile.imread(io.BytesIO(data))))


def _s1_band_masks(band, data_mask) -> dict:
    """Boolean masks for one band: finite / inside dataMask / at floor / valid.

    A pixel is usable when it is finite, inside the evalscript's dataMask, and
    not sitting at the dB floor the evalscript clamps no-backscatter pixels to.
    """
    band = np.asarray(band, dtype=float)
    finite = np.isfinite(band)
    if data_mask is None:
        masked_in = np.ones(band.shape, dtype=bool)
    else:
        mask = np.asarray(data_mask, dtype=float)
        masked_in = (mask > 0) if mask.shape == band.shape else np.ones(
            band.shape, dtype=bool
        )
    at_floor = band <= cdse.S1_DB_FLOOR
    return {
        "finite": finite,
        "masked_in": masked_in,
        "at_floor": at_floor,
        "valid": finite & masked_in & ~at_floor,
    }


def s1_clip_band_stats(data: bytes, names: tuple = ("VV", "VH")) -> List[dict]:
    """Per-band dB statistics for a decoded Sentinel-1 clip.

    Nodata is explicit: a pixel counts as nodata when the evalscript's dataMask
    is 0, when the value is non-finite, or when it sits at the dB floor the
    evalscript clamps to (cdse.S1_DB_FLOOR, i.e. no backscatter signal).

    One dict per band with: nodata_pct, nonfinite_pct, the 1st/50th/99th
    percentile in dB and above10_pct (share of *valid* pixels above +10 dB),
    plus the decoded (height, width) shape. Raises if the GeoTIFF cannot be
    decoded.
    """
    bands, data_mask = _decode_s1(data)

    stats: List[dict] = []
    for i, band in enumerate(bands):
        band = np.asarray(band, dtype=float)
        total = int(band.size)
        masks = _s1_band_masks(band, data_mask)
        finite = masks["finite"]
        valid = masks["valid"]
        nodata = ~valid
        nodata_pct = (100.0 * float(nodata.sum()) / total) if total else 0.0
        nonfinite_pct = (100.0 * float((~finite).sum()) / total) if total else 0.0
        if valid.any():
            values = band[valid]
            p1, p50, p99 = (float(v) for v in np.percentile(values, [1, 50, 99]))
            above10_pct = 100.0 * float((values > S1_DB_MAX).sum()) / float(values.size)
        else:
            p1 = p50 = p99 = float("nan")
            above10_pct = 0.0
        stats.append(
            {
                "name": names[i] if i < len(names) else f"band{i}",
                "nodata_pct": nodata_pct,
                "nonfinite_pct": nonfinite_pct,
                "valid_pct": (100.0 * float(valid.sum()) / total) if total else 0.0,
                "p1": p1,
                "p50": p50,
                "p99": p99,
                "above10_pct": above10_pct,
                "shape": (int(band.shape[0]), int(band.shape[1])),
            }
        )
    return stats


def s1_stats_should_warn(stats: List[dict]) -> Optional[str]:
    """Reason string when the clip should be WARN rather than PASS, else None."""
    reasons = []
    for band in stats:
        if band["nodata_pct"] > S1_NODATA_WARN_PCT:
            reasons.append(
                f"{band['name']} nodata={band['nodata_pct']:.1f}%"
                f">{S1_NODATA_WARN_PCT:.0f}%"
            )
        if band["nonfinite_pct"] > S1_NONFINITE_WARN_PCT:
            reasons.append(
                f"{band['name']} non-finite={band['nonfinite_pct']:.2f}%"
                f">{S1_NONFINITE_WARN_PCT:.0f}%"
            )
    return "; ".join(reasons) if reasons else None


def s1_log_ratio_stats(
    post_data: bytes, pre_data: bytes, names: tuple = ("VV", "VH")
) -> List[dict]:
    """Per-band statistics of the dB log-ratio (post_dB - pre_dB).

    Only pixels usable in BOTH clips (finite, inside the dataMask, not at the dB
    floor) take part, so the ratio is not biased by nodata. Reports the
    1st/50th/99th percentile of the difference in dB, the share of overlapping
    pixels below `S1_DB_DROP` (a strong backscatter drop, the flood signature),
    and the share of pixels exactly at the dB floor in each clip.
    """
    post_bands, post_mask = _decode_s1(post_data)
    pre_bands, pre_mask = _decode_s1(pre_data)
    if len(post_bands) != len(pre_bands):
        raise ValueError(
            f"band count differs: post={len(post_bands)} pre={len(pre_bands)}"
        )

    stats: List[dict] = []
    for i, (post_band, pre_band) in enumerate(zip(post_bands, pre_bands)):
        post_band = np.asarray(post_band, dtype=float)
        pre_band = np.asarray(pre_band, dtype=float)
        if post_band.shape != pre_band.shape:
            raise ValueError(
                f"band {i} shape differs: {post_band.shape} != {pre_band.shape}"
            )
        post = _s1_band_masks(post_band, post_mask)
        pre = _s1_band_masks(pre_band, pre_mask)
        total = int(post_band.size)
        overlap = post["valid"] & pre["valid"]
        if overlap.any():
            diff = post_band[overlap] - pre_band[overlap]
            p1, p50, p99 = (float(v) for v in np.percentile(diff, [1, 50, 99]))
            below_pct = 100.0 * float((diff < S1_DB_DROP).sum()) / float(diff.size)
        else:
            p1 = p50 = p99 = float("nan")
            below_pct = 0.0
        stats.append(
            {
                "name": names[i] if i < len(names) else f"band{i}",
                "overlap_pct": (100.0 * float(overlap.sum()) / total) if total else 0.0,
                "p1": p1,
                "p50": p50,
                "p99": p99,
                "below_drop_pct": below_pct,
                "floor_post_pct": (
                    100.0 * float(post["at_floor"].sum()) / total
                ) if total else 0.0,
                "floor_pre_pct": (
                    100.0 * float(pre["at_floor"].sum()) / total
                ) if total else 0.0,
            }
        )
    return stats


def s1_log_ratio_should_warn(stats: List[dict]) -> Optional[str]:
    """Reason when a band's log-ratio median is not near 0 dB, else None."""
    reasons = [
        f"{band['name']} median={band['p50']:+.2f}dB "
        f"off 0\u00b1{S1_LOG_RATIO_MEDIAN_TOLERANCE_DB:g}dB"
        for band in stats
        if not math.isfinite(band["p50"])
        or abs(band["p50"]) > S1_LOG_RATIO_MEDIAN_TOLERANCE_DB
    ]
    return "; ".join(reasons) if reasons else None


def s1_clip_grid(data: bytes) -> dict:
    """Shape and WGS84 bounds of a clip, read from the GeoTIFF georeferencing."""
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as ds:
        bounds = ds.bounds
        return {
            "shape": (int(ds.height), int(ds.width)),
            "bounds": tuple(
                round(float(v), 9)
                for v in (bounds.left, bounds.bottom, bounds.right, bounds.top)
            ),
            "crs": ds.crs.to_string() if ds.crs else None,
        }


def grids_match(
    post_grid: dict, pre_grid: dict, tolerance: float = 1e-6
) -> Optional[str]:
    """None when both clips share one grid (shape and bounds), else the reason."""
    if post_grid["shape"] != pre_grid["shape"]:
        return f"shape {post_grid['shape']} != {pre_grid['shape']}"
    a, b = post_grid["bounds"], pre_grid["bounds"]
    if any(abs(x - y) > tolerance for x, y in zip(a, b)):
        return f"bounds {a} != {b}"
    return None


def select_s2_scene(scenes: List[dict], event_date: str) -> Optional[dict]:
    """The single S2 selection rule used by both the catalog row and the clip.

    Prefer the clearest scene acquired on/after the event; fall back to the
    clearest scene overall when there is no post-event acquisition.
    """
    if not scenes:
        return None
    on_after = [s for s in scenes if (s.get("acquisition_time") or "")[:10] >= event_date]
    pool = on_after or scenes
    return min(
        pool,
        key=lambda s: (s.get("cloud_pct") if s.get("cloud_pct") is not None else 100.0),
    )


def ohsome_extract_data_timestamp(result: dict) -> Optional[str]:
    """Newest OSM edit timestamp across a v2 extract's cached parquet files."""
    newest: Optional[str] = None
    for path in (result.get("parquet_paths") or {}).values():
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        stamp = ohsome.max_edit_timestamp(data)
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return newest


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
    pre_scene: Optional[dict] = None

    def _pairing():
        nonlocal post_scene, pre_scene
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
        pre_scene = report["pre_scene"]
        return PASS, (
            f"post={report['post_date']} (first on/after {event_date}) "
            f"pre={report['pre_date']} gap={report['gap_days']}d "
            f"(expected {report['expected_days']}d) orbit={report['orbit']} "
            f"dir={report['direction']} pre_scenes={report['pre_count']}"
        )
    runner.run("s1 pairing rule (repeat gap)", _pairing)

    # 5. S1 clip — cold fetch with the on-disk cache cleared, then a cached re-fetch
    s1_res = app_config.fetch.resolution_m

    def _s1_clip():
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if post_scene is None:
            return SKIP, "no post scene"
        start = time.time()
        clip = cdse.fetch_s1_clip(bbox, post_scene)
        cold_s = time.time() - start
        if clip.from_cache:
            # Something was cached for this key: clear it and measure a real
            # cold fetch instead of a disk read.
            clip.path.unlink()
            start = time.time()
            clip = cdse.fetch_s1_clip(bbox, post_scene)
            cold_s = time.time() - start
        if clip.from_cache:
            return FAIL, "cache was not cleared; clip was still served from disk"
        if clip.data[:4] not in (b"II*\x00", b"MM\x00*"):
            return FAIL, "clip is not a GeoTIFF"
        head = (
            f"COLD {cold_s:.1f}s {len(clip.data)} bytes "
            f"{clip.width}x{clip.height} at {s1_res:g}m (cache cleared)"
        )
        try:
            stats = s1_clip_band_stats(clip.data)
        except Exception as exc:  # noqa: BLE001
            return WARN, f"{head} decode-failed({type(exc).__name__})"
        decoded = f"{stats[0]['shape'][1]}x{stats[0]['shape'][0]}"
        bands = "; ".join(
            f"{s['name']} nodata={s['nodata_pct']:.1f}% "
            f"p1/p50/p99={s['p1']:.1f}/{s['p50']:.1f}/{s['p99']:.1f} dB "
            f">{S1_DB_MAX:.0f}dB={s['above10_pct']:.2f}% nan={s['nonfinite_pct']:.2f}%"
            for s in stats
        )
        detail = f"{head} decoded={decoded} | {bands}"
        reason = s1_stats_should_warn(stats)
        if reason:
            return WARN, f"{detail} [WARN {reason}]"
        return PASS, detail
    runner.run("s1 clip cold fetch (cache cleared)", _s1_clip)

    def _s1_clip_cached():
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if post_scene is None:
            return SKIP, "no post scene"
        start = time.time()
        clip = cdse.fetch_s1_clip(bbox, post_scene)
        cached_s = time.time() - start
        if not clip.from_cache:
            return FAIL, "re-fetch was not served from the disk cache"
        return PASS, (
            f"CACHED {cached_s:.3f}s {len(clip.data)} bytes "
            f"{clip.width}x{clip.height} at {s1_res:g}m (no download)"
        )
    runner.run("s1 clip cached re-fetch", _s1_clip_cached)

    # 6. S1 pre-event clip (pairing rule): same grid + post/pre dB log-ratio
    def _s1_pre_log_ratio():
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if post_scene is None or pre_scene is None:
            return SKIP, "no post/pre scene from the pairing rule"
        post = cdse.fetch_s1_clip(bbox, post_scene)
        pre = cdse.fetch_s1_clip(bbox, pre_scene)
        post_grid = s1_clip_grid(post.data)
        pre_grid = s1_clip_grid(pre.data)
        head = (
            f"pre={pre_scene['scene_id'][:32]} ({pre_scene['acquisition_time'][:10]}) "
            f"grid={post_grid['shape'][0]}x{post_grid['shape'][1]} "
            f"bounds={post_grid['bounds']} pre_bytes={len(pre.data)} "
            f"pre_cache={'hit' if pre.from_cache else 'download'}"
        )
        mismatch = grids_match(post_grid, pre_grid)
        if mismatch:
            return FAIL, f"{head} [FAIL post/pre clips do not share one grid: {mismatch}]"
        try:
            stats = s1_log_ratio_stats(post.data, pre.data)
        except Exception as exc:  # noqa: BLE001 - a bad decode must not crash the table
            return WARN, f"{head} decode-failed({type(exc).__name__})"
        bands = "; ".join(
            f"{s['name']} p1/p50/p99={s['p1']:+.2f}/{s['p50']:+.2f}/{s['p99']:+.2f} dB "
            f"<-{abs(S1_DB_DROP):g}dB={s['below_drop_pct']:.2f}% "
            f"floor post/pre={s['floor_post_pct']:.2f}/{s['floor_pre_pct']:.2f}% "
            f"overlap={s['overlap_pct']:.1f}%"
            for s in stats
        )
        detail = f"{head} | {bands}"
        reason = s1_log_ratio_should_warn(stats)
        if reason:
            return WARN, f"{detail} [WARN {reason}]"
        return PASS, detail
    runner.run("s1 pre clip + log-ratio (post-pre dB)", _s1_pre_log_ratio)

    # 7. S2 catalog (same selection rule as the clip below)
    s2_scenes: List[dict] = []

    def _s2_catalog():
        nonlocal s2_scenes
        s2_scenes = cdse.search_sentinel2_scenes(bbox, search_start, search_end, max_cloud=100)
        if not s2_scenes:
            return FAIL, "no S2 L2A scenes in window"
        chosen = select_s2_scene(s2_scenes, event_date)
        return PASS, (
            f"{len(s2_scenes)} scenes; selected (same rule as the clip) "
            f"id={chosen['scene_id']} date={chosen['acquisition_time'][:10]} "
            f"cloud={chosen.get('cloud_pct')}%"
        )
    runner.run("s2 catalog search", _s2_catalog)

    # 8. S2 clip + SCL histogram + cloud estimate
    def _s2_cloud():
        if args.skip_clips:
            return SKIP, "--skip-clips"
        if not creds:
            return SKIP, "no credentials"
        if not s2_scenes:
            return SKIP, "no S2 scenes"
        scene = select_s2_scene(s2_scenes, event_date)
        res = app_config.fetch.resolution_m * 2
        clip = cdse.fetch_s2_scl_clip(bbox, scene, resolution_m=res)
        scl = cdse.read_raster_band(clip.data)
        hist = cdse.scl_class_histogram(scl)
        pct = cdse.estimate_cloud_pct(clip)
        counts = " ".join(f"{k}:{v}" for k, v in hist["counts"].items())
        cloud_txt = "None (undefined)" if pct is None else f"{pct}%"
        detail = (
            f"id={scene['scene_id']} date={scene['acquisition_time'][:10]} "
            f"SCL {len(clip.data)} bytes {clip.width}x{clip.height} at {res:g}m "
            f"cached={clip.from_cache} valid={hist['valid_pct']:.1f}% "
            f"cloud={cloud_txt} (catalog={scene.get('cloud_pct')}%) "
            f"hist[0..11]=[{counts}]"
        )
        if pct is None:
            return WARN, f"{detail} [WARN zero valid SCL pixels: cloud cover undefined]"
        return PASS, detail
    runner.run("s2 scl clip + cloud estimate", _s2_cloud)

    # 9. ohsome
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
        time_form = (fc.get("time_window") or {}).get("form", "unknown")
        # The real data timestamps: the newest OSM edit in the returned extract,
        # and the latest snapshot the ohsome instance reports (via /metadata).
        extract_ts = ohsome_extract_data_timestamp(fc)
        try:
            latest = ohsome.latest_osm_snapshot(ohsome.fetch_ohsome_metadata())
        except DyotakError as exc:  # noqa: PERF203 - provenance is best-effort
            latest = f"unavailable({type(exc).__name__})"
        pre_event = bool(extract_ts) and extract_ts[:10] < event_date
        detail = (
            f"requested={osm_snapshot} format={fc.get('format')} "
            f"time_form={time_form} extract_data_ts={extract_ts} "
            f"(before {event_date}: {pre_event}) "
            f"ohsome_latest_snapshot={latest} counts={counts}"
        )
        if total <= 0:
            return FAIL, detail
        if not pre_event:
            return FAIL, f"{detail} [FAIL extract edit timestamp not before {event_date}]"
        return PASS, detail
    runner.run("ohsome pre-event osm", _ohsome)

    # 10. DEM (rasterio windowed HTTP range reads)
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

    # 11. cache round-trip
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
