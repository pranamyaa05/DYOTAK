"""Guard: /app must never import the offline /eval package or read reference data.

Compliance rule (docs/PLAN.md rule 7, .cursorrules rule 4):
EMSR927 / Copernicus EMS reference data lives in /eval/reference and is
check-only; it is never imported or read by the production application.
"""

import re
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[2] / "app"

# `import eval`, `from eval import ...`, `from eval.foo import ...`
EVAL_PACKAGE_IMPORT = re.compile(
    r"^\s*(?:from\s+eval(?:\.\s*|\s+import\b)|import\s+eval\b)"
)
# Reading reference polygons from anywhere in the app.
REFERENCE_READ = re.compile(r"(eval[\\/]reference|reference[\\/][\w.]+\.(?:geojson|json|gpkg|shp))")


def _app_sources():
    assert APP_DIR.is_dir(), f"missing app dir: {APP_DIR}"
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_app_does_not_import_eval_package():
    offenders = []
    for path in _app_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if EVAL_PACKAGE_IMPORT.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "app imports the offline eval package:\n" + "\n".join(offenders)


def test_app_does_not_read_reference_polygons():
    offenders = []
    for path in _app_sources():
        text = path.read_text(encoding="utf-8")
        for match in REFERENCE_READ.finditer(text):
            offenders.append(f"{path}: {match.group(0)}")
    assert not offenders, "app reads eval reference data:\n" + "\n".join(offenders)
