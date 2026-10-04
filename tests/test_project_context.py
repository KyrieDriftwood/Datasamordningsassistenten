"""Tester för backend/project_context.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.project_context import ProjectContext, ProjectContextError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"


def test_from_path_accepts_existing_directory(tmp_path):
    context = ProjectContext.from_path(tmp_path)
    assert context.root == tmp_path.resolve()


def test_from_path_rejects_missing_path(tmp_path):
    missing = tmp_path / "finns-inte"
    with pytest.raises(ProjectContextError):
        ProjectContext.from_path(missing)


def test_from_path_rejects_file_as_root(tmp_path):
    a_file = tmp_path / "fil.txt"
    a_file.write_text("innehåll")
    with pytest.raises(ProjectContextError):
        ProjectContext.from_path(a_file)


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_list_top_level_entries_returns_subdirectories():
    context = ProjectContext.from_path(EXAMPLES_DIR)
    entries = context.list_top_level_entries()
    names = {entry.name for entry in entries}
    assert "dev phase 0.2" in names
    assert "dev phase Fas 0.1" in names
