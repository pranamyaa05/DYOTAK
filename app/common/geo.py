"""Geographical calculation utilities for DYOTAK."""

import math
from typing import List, Tuple


def calculate_bbox_area_km2(bbox: List[float]) -> float:
    """Compute approximate geodesic area of WGS84 bounding box in km2.

    bbox: [min_lon, min_lat, max_lon, max_lat]
    """
    min_lon, min_lat, max_lon, max_lat = bbox

    # Earth radius in kilometers
    r = 6371.0

    # Convert degrees to radians
    lat1 = math.radians(min_lat)
    lat2 = math.radians(max_lat)
    lon1 = math.radians(min_lon)
    lon2 = math.radians(max_lon)

    # Spherical area formula
    area = (r ** 2) * abs(lon2 - lon1) * abs(math.sin(lat2) - math.sin(lat1))
    return round(area, 2)


def get_utm_epsg_for_bbox(bbox: List[float]) -> int:
    """Determine best UTM EPSG code from centroid of bounding box.

    Adheres to ARCHITECTURE.md 4.6 (UTM zone computed, not hardcoded).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    centroid_lon = (min_lon + max_lon) / 2.0
    centroid_lat = (min_lat + max_lat) / 2.0

    zone_number = int((centroid_lon + 180) / 6) + 1
    if centroid_lat >= 0:
        return 32600 + zone_number  # WGS 84 / UTM North
    else:
        return 32700 + zone_number  # WGS 84 / UTM South


def get_bbox_centroid(bbox: List[float]) -> Tuple[float, float]:
    """Return (lon, lat) of the centroid."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return ((min_lon + max_lon) / 2.0, (min_lat + max_lat) / 2.0)
