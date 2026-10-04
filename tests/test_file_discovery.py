"""Tester för backend/file_discovery.py.

Verifierar sök-/klassificeringslogik mot baslinjen där beteendet är
oförändrat, samt de avsiktliga avvikelserna: PDF döljs i explorerträdet
och det verifierade DGN-gapet är fortsatt bevarat.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from backend.file_discovery import (  # noqa: E402
    DGN_EXTENSION,
    DWG_EXTENSION,
    PDF_EXTENSION,
    EntryKind,
    classify_entry,
    find_documents,
    find_dwg_files,
    list_directory_entries,
    should_ignore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"


def _baseline_find_documents(base_path, recursive=False):
    from src.version_1 import find_documents as baseline_find_documents

    return baseline_find_documents(base_path, recursive=recursive)


def _baseline_list_dwg_files(paths):
    from src.version_5 import list_dwg_files as baseline_list_dwg_files

    return baseline_list_dwg_files(paths)


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_find_documents_matches_baseline_recursive():
    new_result = find_documents(EXAMPLES_DIR, recursive=True)
    baseline_result = _baseline_find_documents(EXAMPLES_DIR, recursive=True)
    assert new_result == baseline_result
    assert len(new_result) > 0, "Förväntade minst en DOCX/XLSX-fixture i Examples/"


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_find_dwg_files_matches_baseline():
    new_result = find_dwg_files(EXAMPLES_DIR, recursive=True)

    all_candidates = list(EXAMPLES_DIR.rglob("*"))
    baseline_result = list(_baseline_list_dwg_files(all_candidates))

    assert new_result == baseline_result
    assert len(new_result) > 0, "Förväntade minst en DWG-fixture i Examples/"


def test_recursive_search_skips_hidden_backup_directory(tmp_path):
    """Backupkopiorna i .datasamordning_backup/ (TB, CBE) får aldrig
    dyka upp som vanliga projektfiler i rekursiva sökningar."""
    from dwg.backup import BACKUP_DIR_NAME

    (tmp_path / "Ritningar").mkdir()
    (tmp_path / "Ritningar" / "X.dwg").write_bytes(b"original")
    (tmp_path / "Ritningar" / "Rapport.docx").write_bytes(b"original")

    backup_dir = tmp_path / BACKUP_DIR_NAME / "Ritningar"
    backup_dir.mkdir(parents=True)
    (backup_dir / "X.dwg").write_bytes(b"backup")
    (backup_dir / "Rapport.docx").write_bytes(b"backup")

    assert find_dwg_files(tmp_path, recursive=True) == [tmp_path / "Ritningar" / "X.dwg"]
    assert find_documents(tmp_path, recursive=True) == [tmp_path / "Ritningar" / "Rapport.docx"]


def test_should_ignore_matches_baseline_prefixes():
    assert should_ignore("~$Document.docx") is True
    assert should_ignore(".hidden-folder") is True
    assert should_ignore("Vanlig-fil.docx") is False


def test_list_directory_entries_hides_generated_pdfs(tmp_path):
    source = tmp_path / "underlag.docx"
    source.touch()
    source.with_suffix(".pdf").touch()

    assert list_directory_entries(tmp_path) == [source.resolve()]


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_classify_entry_covers_all_known_extensions():
    """Kontrollerar att varje riktig fixturefil i Examples/ klassificeras
    likadant som baslinjens `_create_tree_item` skulle ha gjort:
    .docx/.xlsx -> DOCUMENT, .pdf -> PDF, .dwg -> DWG.
    """
    found_kinds: set[EntryKind] = set()
    for entry in EXAMPLES_DIR.rglob("*"):
        if entry.is_dir() or should_ignore(entry.name):
            continue
        kind = classify_entry(entry)
        found_kinds.add(kind)
        suffix = entry.suffix.lower()
        if suffix in {".docx", ".xlsx"}:
            assert kind == EntryKind.DOCUMENT
        elif suffix == PDF_EXTENSION:
            assert kind == EntryKind.PDF
        elif suffix == DWG_EXTENSION:
            assert kind == EntryKind.DWG

    assert EntryKind.DOCUMENT in found_kinds
    assert EntryKind.DWG in found_kinds


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_dgn_fixture_is_verified_gap_not_silently_supported():
    """En riktig .dgn-fixture (Examples/dev phase Fas 0.1/...dgn) måste
    klassificeras som OTHER idag, precis som i baslinjen (som inte
    hanterar DGN alls) — se docs/kravsparning.md, verifierat gap.
    Denna assert ska medvetet gå sönder den dag DGN-stöd faktiskt
    implementeras, som en påminnelse om att uppdatera testet och
    kravsparning.md tillsammans.
    """
    dgn_fixtures = [p for p in EXAMPLES_DIR.rglob(f"*{DGN_EXTENSION}") if not should_ignore(p.name)]
    assert len(dgn_fixtures) > 0, "Förväntade minst en .dgn-fixture i Examples/"
    for fixture in dgn_fixtures:
        assert classify_entry(fixture) == EntryKind.OTHER


@pytest.mark.skipif(not EXAMPLES_DIR.is_dir(), reason="Examples/ finns inte i denna checkout")
def test_list_directory_entries_is_non_recursive_and_sorted():
    entries = list_directory_entries(EXAMPLES_DIR)
    # Endast en nivå: subkatalogerna själva ska synas, inte deras innehåll.
    assert all(entry.parent == EXAMPLES_DIR.resolve() for entry in entries)
    # Mappar (dev phase 0.2, dev phase Fas 0.1) ska sorteras före ev. filer
    # direkt i Examples/, och sinsemellan skiftlägesokänsligt på namn.
    is_dir_flags = [entry.is_dir() for entry in entries]
    assert is_dir_flags == sorted(is_dir_flags, reverse=True)
