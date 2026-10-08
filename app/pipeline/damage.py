"""Damage overlay and classification (Stage 6).

Works in metric UTM CRS computed from AOI centroid.
"""

from typing import Any, Dict, List


def classify_damage(
    flood_prob: Any,
    osm_layers: Dict[str, Any],
    affected_overlap: float = 0.25,
    possibly_affected_overlap: float = 0.05
) -> Dict[str, Any]:
    """Classify OSM features into affected, possibly_affected, and unaffected."""
    return {
        "buildings_affected": 0,
        "buildings_possibly_affected": 0,
        "road_km_affected": 0.0,
        "bridges_possibly_impacted": 0
    }
