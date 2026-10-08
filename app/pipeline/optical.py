"""Sentinel-2 optical indices and cloud screening."""

from typing import Dict, Any


def check_s2_cloud_cover(cloud_pct: float, max_allowed: float = 20.0) -> Dict[str, Any]:
    """Check if Sentinel-2 optical imagery is clear enough for debris/cue extraction."""
    is_clear = cloud_pct <= max_allowed
    return {
        "usable": is_clear,
        "cloud_pct": cloud_pct,
        "threshold": max_allowed,
        "banner": None if is_clear else "Radar only"
    }
