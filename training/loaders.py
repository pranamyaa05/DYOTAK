"""Dataset loaders for Kuro Siwo and Sen1Floods11 splits."""

from pathlib import Path
from typing import Dict, Any


def load_dataset_split(split_name: str, config_path: str = "training/splits.yaml") -> Dict[str, Any]:
    """Load dataset split definitions."""
    return {"split": split_name, "scenes": []}
