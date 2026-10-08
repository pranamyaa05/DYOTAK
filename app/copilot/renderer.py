"""Deterministic template renderer with Devanagari digit support."""

from typing import Dict, Any

NEPALI_DIGITS = {
    "0": "०", "1": "१", "2": "२", "3": "३", "4": "४",
    "5": "५", "6": "६", "7": "७", "8": "८", "9": "९"
}


def to_nepali_digits(num_str: str) -> str:
    return "".join(NEPALI_DIGITS.get(ch, ch) for ch in str(num_str))


def render_copilot_template(template: str, values: Dict[str, Any], lang: str = "en") -> str:
    """Safely interpolate values into validated placeholder string."""
    formatted_values = {}
    for k, v in values.items():
        val_str = str(v)
        if lang == "ne":
            val_str = to_nepali_digits(val_str)
        formatted_values[k] = val_str

    result = template
    for k, v in formatted_values.items():
        result = result.replace(f"{{{k}}}", v)
    return result
