"""Shared pytest fixtures."""
from pathlib import Path
import pytest


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Fresh SQLite file path inside a per-test tmp dir."""
    return tmp_path / "projects.db"
