"""Tester för backend/naming.py (DWG-namnbyte)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from backend.naming import NamingError, rename_dwg_file, rename_many_dwg_files  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"
DWG_FIXTURES = list(EXAMPLES_DIR.rglob("*.dwg")) if EXAMPLES_DIR.is_dir() else []


def _baseline_rename_dwg_file(path, new_name):
    from src.version_5 import rename_dwg_file as baseline_rename_dwg_file

    return baseline_rename_dwg_file(path, new_name)


@pytest.mark.skipif(not DWG_FIXTURES, reason="Ingen DWG-fixture hittades i Examples/")
def test_rename_dwg_file_matches_baseline_behaviour(tmp_path):
    fixture = DWG_FIXTURES[0]
    new_dir = tmp_path / "new"
    baseline_dir = tmp_path / "baseline"
    new_dir.mkdir()
    baseline_dir.mkdir()
    new_copy = new_dir / fixture.name
    baseline_copy = baseline_dir / fixture.name
    shutil.copy2(fixture, new_copy)
    shutil.copy2(fixture, baseline_copy)

    new_result = rename_dwg_file(new_copy, "Nytt-Namn-åäö")
    baseline_result = _baseline_rename_dwg_file(baseline_copy, "Nytt-Namn-åäö")

    assert new_result.name == baseline_result.name == "Nytt-Namn-åäö.dwg"
    assert new_result.is_file()
    assert baseline_result.is_file()


def test_rename_dwg_file_rejects_non_dwg(tmp_path):
    non_dwg = tmp_path / "fil.txt"
    non_dwg.write_text("innehåll")
    with pytest.raises(NamingError):
        rename_dwg_file(non_dwg, "nytt-namn")


def test_rename_dwg_file_rejects_missing_file(tmp_path):
    with pytest.raises(NamingError):
        rename_dwg_file(tmp_path / "finns-inte.dwg", "nytt-namn")


def test_rename_dwg_file_rejects_forbidden_characters(tmp_path):
    fixture = tmp_path / "original.dwg"
    fixture.write_bytes(b"not a real dwg, just a fixture placeholder")
    with pytest.raises(NamingError):
        rename_dwg_file(fixture, "ogiltigt:namn")


def test_rename_dwg_file_rejects_empty_name(tmp_path):
    fixture = tmp_path / "original.dwg"
    fixture.write_bytes(b"not a real dwg, just a fixture placeholder")
    with pytest.raises(NamingError):
        rename_dwg_file(fixture, "   ")


def test_rename_dwg_file_rejects_existing_target(tmp_path):
    fixture = tmp_path / "original.dwg"
    fixture.write_bytes(b"not a real dwg, just a fixture placeholder")
    existing = tmp_path / "upptaget.dwg"
    existing.write_bytes(b"already here")
    with pytest.raises(NamingError):
        rename_dwg_file(fixture, "upptaget")


def test_rename_dwg_file_is_noop_for_same_name(tmp_path):
    fixture = tmp_path / "oforandrad.dwg"
    fixture.write_bytes(b"not a real dwg, just a fixture placeholder")
    result = rename_dwg_file(fixture, "oforandrad")
    assert result == fixture
    assert fixture.is_file()


def test_rename_dwg_file_accepts_name_with_extension(tmp_path):
    fixture = tmp_path / "original.dwg"
    fixture.write_bytes(b"not a real dwg, just a fixture placeholder")
    result = rename_dwg_file(fixture, "nytt-namn.dwg")
    assert result.name == "nytt-namn.dwg"


def test_rename_many_dwg_files(tmp_path):
    first = tmp_path / "forsta.dwg"
    second = tmp_path / "andra.dwg"
    first.write_bytes(b"placeholder-1")
    second.write_bytes(b"placeholder-2")

    renamed = rename_many_dwg_files({first: "ny-forsta", second: "ny-andra"})

    assert renamed[first].name == "ny-forsta.dwg"
    assert renamed[second].name == "ny-andra.dwg"
    assert renamed[first].is_file()
    assert renamed[second].is_file()
