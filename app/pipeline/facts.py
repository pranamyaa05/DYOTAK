"""Facts builder (Stage 8).

Constructs facts.json adhering to ARCHITECTURE.md Section 7.
facts.json is the single source of truth for all numbers shown to users.
"""

from typing import List
from contracts.schemas import (
    CutoffSettlementRecord,
    FactItem,
    FactsJSON,
    FactsMeta,
    FactsTables,
    PipelineStage,
    Provenance,
)


def build_facts_json(
    job_id: str,
    bbox: List[float],
    event_date: str,
    provenance: Provenance,
    facts: List[FactItem],
    cutoff_settlements: List[CutoffSettlementRecord]
) -> FactsJSON:
    """Build canonical FactsJSON payload from computed pipeline outputs."""
    meta = FactsMeta(
        job_id=job_id,
        bbox=bbox,
        event_date=event_date,
        provenance=provenance,
        units={
            "area": "km2",
            "distance": "km",
            "count": "integer"
        }
    )
    tables = FactsTables(cutoff_settlements=cutoff_settlements)
    return FactsJSON(meta=meta, facts=facts, tables=tables)
