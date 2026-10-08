"""Situation report download router (ARCHITECTURE.md Section 10)."""

from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Response
from app.report.renderer import render_report_html
from contracts.schemas import FactsJSON

router = APIRouter(prefix="/jobs/{job_id}/report", tags=["report"])


@router.get("")
def get_report(job_id: str, lang: str = Query(default="en", pattern="^(en|ne)$")):
    """Generate or retrieve one-page situation report."""
    facts_path = Path("contracts/examples/facts.json")
    if not facts_path.is_file():
        raise HTTPException(status_code=404, detail="Facts not available for report generation")

    with open(facts_path, "r", encoding="utf-8") as f:
        facts = FactsJSON.model_validate_json(f.read())
        facts.meta.job_id = job_id

    html_content = render_report_html(facts, lang=lang)
    return Response(content=html_content, media_type="text/html")
