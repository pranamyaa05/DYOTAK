"""Copilot tools for read-only access to facts.json."""

from typing import Any, Dict, Optional
from contracts.schemas import FactsJSON


def get_fact_by_id(facts_doc: FactsJSON, fact_id: str) -> Optional[Any]:
    for item in facts_doc.facts:
        if item.id == fact_id:
            return item.value
    return None
