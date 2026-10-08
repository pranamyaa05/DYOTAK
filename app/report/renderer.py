"""Situation report HTML/PDF renderer."""

from pathlib import Path
from typing import Any, Dict
from contracts.schemas import FactsJSON

TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_report_html(facts: FactsJSON, lang: str = "en") -> str:
    """Render HTML situation report from facts and template."""
    template_file = TEMPLATES_DIR / f"{lang}.html"
    if not template_file.is_file():
        template_file = TEMPLATES_DIR / "en.html"

    with open(template_file, "r", encoding="utf-8") as f:
        html = f.read()

    # Populate facts mapping
    fact_dict = {f.id: str(f.value) for f in facts.facts}
    for k, v in fact_dict.items():
        html = html.replace(f"{{{{ {k} }}}}", v)

    html = html.replace("{{ job_id }}", facts.meta.job_id)
    html = html.replace("{{ event_date }}", facts.meta.event_date)
    return html
