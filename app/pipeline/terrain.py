"""Terrain conditioning and masking (Stage 4).

Computes slope, HAND, and radar shadow/layover masks.
"""

from typing import Tuple
import numpy as np


def apply_terrain_masks(
    flood_prob: np.ndarray,
    slope: np.ndarray,
    hand: np.ndarray,
    slope_cutoff: float = 15.0,
    hand_cutoff: float = 15.0
) -> np.ndarray:
    """Mask out false positive flood detections on steep slopes or high HAND terrain."""
    filtered = np.copy(flood_prob)
    mask = (slope > slope_cutoff) | (hand > hand_cutoff)
    filtered[mask] = 0.0
    return filtered
