"""Cache hashing and path resolution according to ARCHITECTURE.md Section 6."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def compute_config_hash(config_dict: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of configuration parameters."""
    encoded = json.dumps(config_dict, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def compute_job_cache_key(
    bbox: List[float],
    event_date: str,
    model_version: str,
    osm_snapshot_date: str,
    config_hash: str
) -> str:
    """Compute artifact cache key: hash(bbox, dates, model_version, osm_snapshot, config_hash)."""
    payload = {
        "bbox": [round(c, 5) for c in bbox],
        "event_date": event_date,
        "model_version": model_version,
        "osm_snapshot_date": osm_snapshot_date,
        "config_hash": config_hash
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def compute_raw_clip_key(bbox: List[float], time_window: str, product: str, version: str) -> str:
    """Compute cache key for raw clips: hash(bbox, time, product, version)."""
    payload = {
        "bbox": [round(c, 5) for c in bbox],
        "time_window": time_window,
        "product": product,
        "version": version
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def raw_cache_path(root: str, namespace: str, key: str, suffix: str = "") -> Path:
    """Resolve the on-disk path for a cached raw artifact.

    Layout: <root>/raw/<namespace>/<key[:2]>/<key><suffix>
    The two-character shard keeps directory fan-out small.
    """
    return Path(root) / "raw" / namespace / key[:2] / f"{key}{suffix}"


def read_cached_bytes(path: Path) -> Optional[bytes]:
    """Return cached bytes, or None if the artifact is not present."""
    if path.is_file():
        return path.read_bytes()
    return None


def write_cached_bytes(path: Path, data: bytes) -> None:
    """Atomically write raw bytes to the cache (temp file then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
