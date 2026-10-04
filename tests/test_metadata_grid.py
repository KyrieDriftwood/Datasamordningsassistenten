"""Tester för metadata/grid.py: multi-dokument-metadatagrid.

Jämförs direkt mot baslinjens ``src/version_4.py:extract_metadata_grid``
på riktiga fixturer (samma DOCX/XLSX-uppsättning som redan används i
test_metadata.py), samt review-watermark-orkestreringen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from metadata.grid import extract_metadata_grid, load_metadata_categories

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples" / "dev phase 0.2"
DOCX_FIXTURES = sorted(EXAMPLES_DIR.glob("*.docx")) if EXAMPLES_DIR.is_dir() else []
XLSX_FIXTURES = sorted(EXAMPLES_DIR.glob("*.xlsx")) if EXAMPLES_DIR.is_dir() else []
ALL_FIXTURES = DOCX_FIXTURES + XLSX_FIXTURES


def _baseline_extract_metadata_grid(paths):
    from src.version_4 import extract_metadata_grid as baseline_extract

    return baseline_extract(paths)


def _grid_as_comparable(grid):
    """Reducerar en MetadataGrid till jämförbara primitiver (dataclass-
    identitet skiljer sig mellan de två modulerna även om innehållet är
    detsamma, eftersom MetadataGrid/MetadataColumn/MetadataRow är
    separat definierade i varje modul)."""
    rows = tuple((row.key, row.label, row.document_types) for row in grid.rows)
    columns = tuple(
        (
            column.path,
            column.document_type,
            column.error,
            tuple(sorted((key, field.key, field.label, field.value, field.editable) for key, field in column.fields.items())),
        )
        for column in grid.columns
    )
    return rows, columns


def test_extract_metadata_grid_matches_baseline_on_real_fixtures():
    if not ALL_FIXTURES:
        import pytest

        pytest.skip("Inga DOCX/XLSX-fixturer hittades under .old/python_v5/tests/fixtures")

    baseline_grid = _baseline_extract_metadata_grid(ALL_FIXTURES)
    new_grid = extract_metadata_grid(ALL_FIXTURES)

    assert _grid_as_comparable(new_grid) == _grid_as_comparable(baseline_grid)


def test_load_metadata_categories_matches_baseline_fallback_list():
    import pytest

    baseline = pytest.importorskip("src.version_4", reason="Private historical baseline is not included")

    baseline_categories = baseline.load_metadata_categories()
    new_categories = load_metadata_categories()

    assert tuple((c.key, c.label) for c in new_categories) == tuple((c.key, c.label) for c in baseline_categories)


def test_metadata_cache_reads_unchanged_file_once_and_rereads_after_change(tmp_path, monkeypatch):
    import os
    import shutil

    from metadata import grid as grid_module

    if not XLSX_FIXTURES:
        import pytest

        pytest.skip("Inga XLSX-fixturer")
    working_copy = tmp_path / XLSX_FIXTURES[0].name
    shutil.copyfile(XLSX_FIXTURES[0], working_copy)
    grid_module.clear_metadata_cache()
    reads: list[Path] = []
    original = grid_module.extract_metadata

    def counting_extract(path):
        reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(grid_module, "extract_metadata", counting_extract)

    first = extract_metadata_grid([working_copy])
    extract_metadata_grid([working_copy])
    assert len(reads) == 1

    status = working_copy.stat()
    os.utime(working_copy, ns=(status.st_atime_ns, status.st_mtime_ns + 10_000_000))
    second = extract_metadata_grid([working_copy])
    assert len(reads) == 2
    assert _grid_as_comparable(first) == _grid_as_comparable(second)
