"""Tester för metadata-paketet (metadata/__init__.py, docx.py, xlsx.py).

Läsvägen jämförs direkt mot baslinjens `version_3.py:extract_metadata`
på riktiga mallfiler i Examples/. Skrivvägen för DOCX kräver Word/COM
(VBScript-automatisering) och testas inte här — se modulens docstring
i metadata/docx.py. Skrivvägen för XLSX (ren XML-manipulation) kan
testas utan externa beroenden och verifieras nedan mot en kopia av en
riktig fixture.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from metadata import DocumentMetadata, MetadataError, extract_metadata, update_metadata  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "Examples"

DOCX_FIXTURES = sorted((EXAMPLES_DIR / "dev phase 0.2").glob("*.docx")) if EXAMPLES_DIR.is_dir() else []
XLSX_FIXTURES = sorted((EXAMPLES_DIR / "dev phase 0.2").glob("*.xlsx")) if EXAMPLES_DIR.is_dir() else []


def _baseline_extract_metadata(path):
    from src.version_3 import extract_metadata as baseline_extract_metadata

    return baseline_extract_metadata(path)


def _as_comparable(document: DocumentMetadata) -> tuple:
    return (
        document.path,
        document.document_type,
        tuple((field.key, field.label, field.value, field.editable, field.location) for field in document.fields),
    )


@pytest.mark.skipif(not DOCX_FIXTURES, reason="Inga DOCX-fixturer hittades i Examples/")
@pytest.mark.parametrize("fixture_path", DOCX_FIXTURES, ids=lambda p: p.name)
def test_extract_docx_metadata_matches_baseline(fixture_path):
    try:
        baseline_result = _baseline_extract_metadata(fixture_path)
    except Exception as exc:  # baslinjen kan också fela på vissa mallar (t.ex. saknade Content Controls)
        with pytest.raises(type(exc)):
            extract_metadata(fixture_path)
        return

    new_result = extract_metadata(fixture_path)
    baseline_fields = _as_comparable(baseline_result)[2]
    new_fields = _as_comparable(new_result)[2]
    # Avsiktlig avvikelse (2026-10-02, utanför TB): kontroller i sidhuvud/-fot
    # läses också. Baslinjens brödtextfält ska vara oförändrade och först.
    assert _as_comparable(new_result)[:2] == _as_comparable(baseline_result)[:2]
    assert new_fields[: len(baseline_fields)] == baseline_fields
    baseline_keys = {field[0] for field in baseline_fields}
    assert all(field[0] not in baseline_keys for field in new_fields[len(baseline_fields) :])


@pytest.mark.skipif(not XLSX_FIXTURES, reason="Inga XLSX-fixturer hittades i Examples/")
@pytest.mark.parametrize("fixture_path", XLSX_FIXTURES, ids=lambda p: p.name)
def test_extract_xlsx_metadata_matches_baseline(fixture_path):
    try:
        baseline_result = _baseline_extract_metadata(fixture_path)
    except Exception as exc:
        with pytest.raises(type(exc)):
            extract_metadata(fixture_path)
        return

    new_result = extract_metadata(fixture_path)
    assert _as_comparable(new_result) == _as_comparable(baseline_result)


def test_extract_metadata_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_metadata(tmp_path / "finns-inte.docx")


def test_extract_metadata_rejects_unsupported_extension(tmp_path):
    unsupported = tmp_path / "fil.txt"
    unsupported.write_text("innehåll")
    with pytest.raises(ValueError):
        extract_metadata(unsupported)


@pytest.mark.skipif(not XLSX_FIXTURES, reason="Inga XLSX-fixturer hittades i Examples/")
def test_update_metadata_writes_editable_xlsx_field(tmp_path):
    fixture_path = XLSX_FIXTURES[0]
    working_copy = tmp_path / fixture_path.name
    shutil.copy2(fixture_path, working_copy)

    document = extract_metadata(working_copy)
    editable_field = next((field for field in document.fields if field.editable), None)
    if editable_field is None:
        pytest.skip(f"{fixture_path.name} saknar redigerbara fält att testa mot")

    new_value = "TESTVÄRDE-åäö"
    update_metadata(working_copy, {editable_field.key: new_value})

    reread = extract_metadata(working_copy)
    updated_field = next(field for field in reread.fields if field.key == editable_field.key)
    assert updated_field.value == new_value


@pytest.mark.skipif(not XLSX_FIXTURES, reason="Inga XLSX-fixturer hittades i Examples/")
def test_update_metadata_rejects_locked_field(tmp_path):
    fixture_path = XLSX_FIXTURES[0]
    working_copy = tmp_path / fixture_path.name
    shutil.copy2(fixture_path, working_copy)

    document = extract_metadata(working_copy)
    locked_field = next((field for field in document.fields if not field.editable), None)
    if locked_field is None:
        pytest.skip(f"{fixture_path.name} saknar beräknade (låsta) fält att testa mot")

    with pytest.raises(ValueError):
        update_metadata(working_copy, {locked_field.key: "ska inte fungera"})


def test_update_metadata_rejects_unknown_key(tmp_path):
    if not XLSX_FIXTURES:
        pytest.skip("Inga XLSX-fixturer hittades i Examples/")
    fixture_path = XLSX_FIXTURES[0]
    working_copy = tmp_path / fixture_path.name
    shutil.copy2(fixture_path, working_copy)

    with pytest.raises(KeyError):
        update_metadata(working_copy, {"DENNA_NYCKEL_FINNS_INTE": "x"})
