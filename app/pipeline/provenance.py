"""Provenance generator for DYOTAK outputs (ARCHITECTURE.md Section 6).

Every result carries provenance: live | cached | degraded(reason).
"""

from datetime import datetime, timezone
from typing import List, Optional
from app.settings import CONFIG_HASH, settings
from contracts.schemas import (
    DegradationItem,
    Provenance,
    ProvenanceMode,
    S2Provenance,
    ScenePairingProvenance,
)


def create_provenance(
    scenes: ScenePairingProvenance,
    osm_snapshot_date: str,
    s2_provenance: Optional[S2Provenance] = None,
    mode: ProvenanceMode = ProvenanceMode.LIVE,
    model_version: str = "dyotak_onnx_v1",
    degradations: Optional[List[DegradationItem]] = None,
    computed_at: Optional[str] = None
) -> Provenance:
    """Construct verified provenance record for any output artifact."""
    if computed_at is None:
        computed_at = datetime.now(timezone.utc).isoformat()

    return Provenance(
        mode=mode,
        computed_at=computed_at,
        code_version=settings.app_version,
        config_hash=CONFIG_HASH,
        model_version=model_version,
        scenes=scenes,
        s2=s2_provenance or S2Provenance(used=False),
        osm_snapshot_date=osm_snapshot_date,
        degradations=degradations or []
    )
