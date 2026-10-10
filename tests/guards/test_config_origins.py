"""Guard: measured tunables carry their origin comment in config/default.yaml."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"


def test_s2_datatake_window_is_configured_with_a_measured_origin():
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    line = next(
        (
            ln
            for ln in text.splitlines()
            if ln.strip().startswith("s2_datatake_window_seconds:")
        ),
        None,
    )
    assert line is not None, "fetch.s2_datatake_window_seconds missing from default.yaml"
    assert "# origin:" in line
    # The value was measured against live CDSE clips, not guessed.
    assert "measured" in line

    config = yaml.safe_load(text)
    # The window must cover a whole Sentinel-2 datatake (~20-25 min).
    assert config["fetch"]["s2_datatake_window_seconds"] >= 20 * 60
