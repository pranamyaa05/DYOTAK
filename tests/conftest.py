"""Shared pytest fixtures for the DYOTAK test suite."""

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"


def load_example(name: str) -> dict:
    """Load a JSON example payload from /contracts/examples."""
    with open(EXAMPLES_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def examples_dir() -> Path:
    return EXAMPLES_DIR


@pytest.fixture(scope="session")
def example() -> "callable":
    return load_example
