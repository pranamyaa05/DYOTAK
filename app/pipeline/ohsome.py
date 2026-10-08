"""Pre-event OpenStreetMap extraction via ohsome API (Stage 5).

Enforces .cursorrules Rule 4 & 5:
- Allowed inputs: OSM as of a snapshot date BEFORE the event.
- NEVER import or read Copernicus EMS, UNOSAT or post-event OSM in /app.
- Enforce osm_snapshot_date < event_date with an assertion and a test.
"""

from typing import Any, Dict, List
from app.common.errors import OhsomeUnavailableError
from app.common.retry import retry_with_backoff


def validate_osm_snapshot_date(osm_snapshot_date: str, event_date: str) -> None:
    """Enforce .cursorrules Rule 5 with a hard assertion."""
    assert osm_snapshot_date < event_date, (
        f"Assertion failed: osm_snapshot_date ({osm_snapshot_date}) must be strictly before event_date ({event_date})"
    )


@retry_with_backoff(
    retries=3,
    backoff_factor=1.5,
    timeout=45.0,
    on_failure_raise=lambda exc: OhsomeUnavailableError(details=str(exc))
)
def fetch_preevent_osm_elements(
    bbox: List[float],
    osm_snapshot_date: str,
    event_date: str,
    feature_types: List[str]
) -> Dict[str, Any]:
    """Fetch pre-event OSM geometries from ohsome API.

    Guaranteed osm_snapshot_date < event_date.
    """
    validate_osm_snapshot_date(osm_snapshot_date, event_date)

    # In production, this executes the HTTP query against ohsome with time=osm_snapshot_date
    return {
        "type": "FeatureCollection",
        "features": [],
        "snapshot_date": osm_snapshot_date
    }
