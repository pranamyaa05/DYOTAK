"""Copilot Answer Validator (ARCHITECTURE.md Section 9).

Validates that:
1. Every placeholder resolves to a known fact ID.
2. No hallucinated digits/numerals appear outside rendered placeholders.
3. All referenced settlement names exist in the facts table.
"""

import re
from typing import Dict, List, Set, Tuple


class CopilotValidationError(Exception):
    pass


def validate_draft_prose(
    draft_template: str,
    valid_fact_ids: Set[str],
    valid_settlement_names: Set[str]
) -> Tuple[bool, List[str]]:
    """Validate raw LLM draft containing placeholders like {flooded_area_km2}."""
    errors = []

    # Check placeholders
    placeholders = re.findall(r"\{([a-zA-Z0-9_]+)\}", draft_template)
    for p in placeholders:
        if p not in valid_fact_ids and p not in valid_settlement_names:
            errors.append(f"Unrecognized placeholder: {{{p}}}")

    # Strip valid placeholders and check if any raw digits exist outside placeholders
    text_without_placeholders = re.sub(r"\{[a-zA-Z0-9_]+\}", "", draft_template)
    digit_matches = re.findall(r"\d+", text_without_placeholders)
    if digit_matches:
        errors.append(f"Forbidden raw numerals outside placeholders detected: {digit_matches}")

    return len(errors) == 0, errors
