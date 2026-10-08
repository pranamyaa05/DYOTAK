"""Guard: the production application must contain no torch (.cursorrules rule 2)."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

TORCH_IMPORT = re.compile(r"^\s*(?:from\s+torch(?:\.\s*|\s+import\b)|import\s+torch\b)")


def _app_sources():
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_app_has_no_torch_import():
    offenders = []
    for path in _app_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TORCH_IMPORT.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "torch imported in /app:\n" + "\n".join(offenders)


def _requirement_tokens(text: str):
    """Yield the package name of each non-comment requirement line."""
    for line in text.splitlines():
        spec = line.split("#", 1)[0].strip()
        if not spec or spec.startswith("-"):  # blank or pip flag (-r, -e)
            continue
        yield re.split(r"[<>=!~\[;\s]", spec, 1)[0].strip().lower()


def test_runtime_requirements_do_not_pin_torch():
    for name in ("requirements.txt", "requirements.lock"):
        req = REPO_ROOT / name
        if not req.is_file():
            continue
        for pkg in _requirement_tokens(req.read_text(encoding="utf-8")):
            assert not (pkg == "torch" or pkg.startswith("torch")), (
                f"{name} pins a torch package ({pkg}); production must not contain torch"
            )


def test_health_reports_no_torch():
    health = TestClient(app).get("/api/health").json()
    assert health["torch_detected"] is False
