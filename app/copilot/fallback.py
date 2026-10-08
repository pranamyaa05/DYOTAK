"""Deterministic fallback answers when LLM is unavailable or fails validation."""

FALLBACK_TEMPLATES = {
    "summary": "Mapped flood area: {flooded_area_km2} km². Affected buildings: {buildings_affected}. Road affected: {road_km_affected} km.",
    "isolation": "{settlements_newly_cut_off} settlements are newly cut off from hospital or town access."
}


def get_fallback_answer(intent: str = "summary") -> str:
    return FALLBACK_TEMPLATES.get(intent, FALLBACK_TEMPLATES["summary"])
