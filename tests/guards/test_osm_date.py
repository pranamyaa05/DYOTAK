"""Guard: OSM snapshots must strictly precede the event date (.cursorrules rule 5)."""

import pytest
from pydantic import ValidationError

from contracts.schemas import JobRequest, PreflightRequest

BBOX = [85.15, 27.85, 85.35, 28.05]


@pytest.mark.parametrize("model", [PreflightRequest, JobRequest])
@pytest.mark.parametrize("snapshot", ["2024-09-28", "2024-09-29", "2024-12-01"])
def test_snapshot_on_or_after_event_rejected(model, snapshot):
    with pytest.raises((ValidationError, AssertionError)):
        model(bbox=BBOX, event_date="2024-09-28", osm_snapshot_date=snapshot)


@pytest.mark.parametrize("model", [PreflightRequest, JobRequest])
@pytest.mark.parametrize("snapshot", ["2024-09-25", "2024-09-27"])
def test_snapshot_before_event_accepted(model, snapshot):
    req = model(bbox=BBOX, event_date="2024-09-28", osm_snapshot_date=snapshot)
    assert req.osm_snapshot_date == snapshot
