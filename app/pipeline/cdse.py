"""CDSE Catalog and Process API client for Sentinel-1 and Sentinel-2 clips."""

from typing import Any, Dict, List
from app.common.errors import CdseUnavailableError
from app.common.retry import retry_with_backoff


@retry_with_backoff(
    retries=3,
    backoff_factor=1.5,
    timeout=60.0,
    on_failure_raise=lambda exc: CdseUnavailableError(details=str(exc))
)
def search_sentinel1_scenes(bbox: List[float], start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Search CDSE catalog for Sentinel-1 GRD IW acquisitions."""
    # Production implementation interfaces CDSE OData API
    return []
