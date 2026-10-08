"""Tests for the G0 spike pairing assertions (scripts/g0_spike.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_spike():
    path = Path(__file__).resolve().parents[2] / "scripts" / "g0_spike.py"
    spec = importlib.util.spec_from_file_location("g0_spike", path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects sys.modules[cls.__module__], so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def _scene(scene_id, when, direction="DESCENDING", orbit=19):
    return {
        "scene_id": scene_id,
        "acquisition_time": when,
        "orbit_direction": direction,
        "relative_orbit": orbit,
    }


def test_pairing_report_picks_expected_pre_post_and_gap():
    scenes = [
        _scene("pre", "2024-09-20T00:25:11Z"),
        _scene("post", "2024-10-02T00:19:42Z"),
        _scene("older-pre", "2024-09-08T00:25:11Z"),
    ]
    report = spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
    assert report["post_date"] == "2024-10-02"
    assert report["pre_date"] == "2024-09-20"
    assert report["gap_days"] == 12
    assert report["expected_days"] == 12
    assert report["orbit"] == 19
    assert report["direction"] == "DESCENDING"


def test_pairing_report_picks_first_scene_after_event():
    scenes = [
        _scene("pre", "2024-09-17T00:25:11Z"),
        _scene("first-post", "2024-09-29T00:19:42Z"),  # 1 day after event
        _scene("later-post", "2024-10-02T00:19:42Z"),
    ]
    report = spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
    assert report["post_date"] == "2024-09-29"


def test_pairing_report_rejects_wrong_gap():
    scenes = [
        _scene("pre", "2024-09-01T00:25:11Z"),   # 31 days before post
        _scene("post", "2024-10-02T00:19:42Z"),
    ]
    with pytest.raises(ValueError):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)


def test_pairing_report_rejects_no_post():
    scenes = [_scene("pre", "2024-09-20T00:25:11Z")]
    with pytest.raises(ValueError):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)


def test_pairing_report_rejects_no_matching_pre_orbit():
    scenes = [
        _scene("pre-asc", "2024-09-20T00:25:11Z", direction="ASCENDING", orbit=19),
        _scene("post", "2024-10-02T00:19:42Z", direction="DESCENDING", orbit=19),
    ]
    # No same-direction pre scene -> pairing rule raises (AssertionError/NoValidOrbitPairError)
    with pytest.raises(Exception):
        spike.s1_pairing_report(scenes, "2024-09-28", repeat_days=12)
