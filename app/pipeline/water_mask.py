"""Permanent-water exclusion masks (Stage 4).

Permanent water is read from the PRE-event image, not from a hydrography
dataset: open water is the darkest land cover in both Sentinel-1 polarizations,
so a pixel is permanent water when the pre-event VV **and** VH medians are both
below the configured thresholds (``flood.permanent_water_vv_max_db`` /
``flood.permanent_water_vh_max_db``, origin comments in config/default.yaml).

Requiring both polarizations keeps surfaces that are dark in one band only
(smooth dry ground, radar shadow, sand) out of the permanent-water class.
Pixels that are NaN (nodata) are never permanent water.

Anything detected as new flooding inside the permanent-water mask is dropped:
the product reports *new* flooding, and a lake that is dark before and after the
event is not new.
"""

from typing import Any, Optional

import numpy as np

from app.settings import app_config


def permanent_water_mask(
    pre_vv: np.ndarray,
    pre_vh: np.ndarray,
    vv_max_db: Optional[float] = None,
    vh_max_db: Optional[float] = None,
    config: Any = None,
) -> np.ndarray:
    """True where the pre-event image shows open water in both polarizations.

    NaN (nodata) pixels are never permanent water.
    """
    cfg = config or app_config
    vv_threshold = float(
        vv_max_db
        if vv_max_db is not None
        else cfg.flood.permanent_water_vv_max_db
    )
    vh_threshold = float(
        vh_max_db
        if vh_max_db is not None
        else cfg.flood.permanent_water_vh_max_db
    )
    vv = np.asarray(pre_vv, dtype="float64")
    vh = np.asarray(pre_vh, dtype="float64")
    if vv.shape != vh.shape:
        raise ValueError("pre_vv and pre_vh must share one grid")
    return (
        np.isfinite(vv)
        & np.isfinite(vh)
        & (vv <= vv_threshold)
        & (vh <= vh_threshold)
    )


def exclude_permanent_water(flood_prob: np.ndarray, permanent_water_mask: np.ndarray) -> np.ndarray:
    """Zero out known permanent water bodies from new flood map."""
    filtered = np.copy(flood_prob)
    filtered[permanent_water_mask > 0] = 0.0
    return filtered
