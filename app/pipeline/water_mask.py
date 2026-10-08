"""Permanent-water exclusion masks."""

import numpy as np


def exclude_permanent_water(flood_prob: np.ndarray, permanent_water_mask: np.ndarray) -> np.ndarray:
    """Zero out known permanent water bodies from new flood map."""
    filtered = np.copy(flood_prob)
    filtered[permanent_water_mask > 0] = 0.0
    return filtered
