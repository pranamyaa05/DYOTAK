"""Preset AOIs router (ARCHITECTURE.md Section 7)."""

from pathlib import Path
from typing import Any, Dict, List
import yaml
from fastapi import APIRouter

router = APIRouter(prefix="/presets", tags=["presets"])
PRESETS_DIR = Path("config/presets")


@router.get("", response_model=List[Dict[str, Any]])
def list_presets() -> List[Dict[str, Any]]:
    """List pre-configured evaluation and demonstration AOIs."""
    presets = []
    if PRESETS_DIR.is_dir():
        for f in PRESETS_DIR.glob("*.yaml"):
            with open(f, "r", encoding="utf-8") as fp:
                data = yaml.safe_load(fp)
                if data:
                    presets.append(data)
    return presets
