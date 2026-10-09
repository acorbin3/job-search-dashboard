"""Shared pytest helpers."""
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_dir():
    return FIXTURE_DIR


@pytest.fixture
def read_fixture(fixture_dir):
    def _read(name: str) -> str:
        return (fixture_dir / name).read_text(encoding="utf-8")
    return _read
