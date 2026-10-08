"""Log-ratio change detection baseline (ARCHITECTURE.md Section 4.3)."""

from typing import Tuple
import numpy as np


def compute_log_ratio_change(
    pre_vv: np.ndarray,
    post_vv: np.ndarray,
    pre_vh: np.ndarray,
    post_vh: np.ndarray,
    threshold_db: float = -3.0
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute difference in dB between post and pre Sentinel-1 VV/VH arrays.

    Returns:
        (flood_mask, difference_db)
    """
    diff_vv = post_vv - pre_vv
    diff_vh = post_vh - pre_vh
    combined_diff = np.minimum(diff_vv, diff_vh)
    flood_mask = (combined_diff <= threshold_db).astype(np.uint8)
    return flood_mask, combined_diff
