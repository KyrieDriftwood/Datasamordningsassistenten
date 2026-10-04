"""Tester för backend/file_status.py (granskningsstatus/vattenstämpling).

Bygger på samma syntetiska DOCX/XLSX-mallhjälpare som baslinjens
``.old/python_v5/tests/test_version_1.py`` använder, och jämför
resultatet direkt mot baslinjens egna ``version_1.py``-funktioner för
att verifiera byte-för-byte-paritet, inte bara "liknande" beteende.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".old" / "python_v5"))

from backend.file_status import (  # noqa: E402
    has_review_watermark,
    process_directory,
    update_docx_status,
    update_document_status,
    update_xlsx_status,
)
def _create_docx_template(path: Path) -> None:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<w:body><w:p><w:r><w:t>START</w:t></w:r></w:p><w:sectPr>"
        '<w:headerReference r:id="rId1"/>'
        "</w:sectPr></w:body>"
        "</w:document>"
    )
    header_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:r><w:t>Header</w:t></w:r></w:p>"
        "</w:hdr>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/header1.xml", header_xml)
        archive.writestr("word/_rels/document.xml.rels", rels_xml)


def _create_xlsx_template(path: Path) -> None:
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>STATUS</t></is></c>'
        "</row></sheetData>"
        "</worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _baseline_update_docx_status(path, status):
    baseline = pytest.importorskip("src.version_1", reason="Private historical baseline is not included")

    return baseline.update_docx_status(path, status)


def _baseline_update_xlsx_status(path, status):
    baseline = pytest.importorskip("src.version_1", reason="Private historical baseline is not included")

    return baseline.update_xlsx_status(path, status)


def _baseline_process_directory(base_path, status, recursive=False):
    baseline = pytest.importorskip("src.version_1", reason="Private historical baseline is not included")

    return baseline.process_directory(base_path, status, recursive=recursive)


def _read_document_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.read("word/document.xml")


def _read_header_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.read("word/header1.xml")


def _read_sheet_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.read("xl/worksheets/sheet1.xml")


@pytest.mark.parametrize("status", ["för granskning", "godkänd"])
def test_update_docx_status_matches_baseline_byte_for_byte(tmp_path, status):
    new_copy = tmp_path / "new.docx"
    baseline_copy = tmp_path / "baseline.docx"
    _create_docx_template(new_copy)
    _create_docx_template(baseline_copy)

    update_docx_status(new_copy, status)
    _baseline_update_docx_status(baseline_copy, status)

    assert _read_header_xml(new_copy) == _read_header_xml(baseline_copy)
    assert _read_document_xml(new_copy) == _read_document_xml(baseline_copy)


def test_update_docx_status_is_idempotent_when_rerun(tmp_path):
    template = tmp_path / "mall.docx"
    _create_docx_template(template)

    update_docx_status(template, "för granskning")
    update_docx_status(template, "godkänd")
    output = update_docx_status(template, "för granskning")

    with zipfile.ZipFile(output, "r") as archive:
        header_xml = archive.read("word/header1.xml").decode("utf-8")
    assert header_xml.count('type="#_x0000_t136"') == 1


def test_review_watermark_detection_tracks_add_and_remove(tmp_path):
    document = tmp_path / "review.docx"
    _create_docx_template(document)

    assert not has_review_watermark(document)

    update_docx_status(document, "för granskning")
    assert has_review_watermark(document)

    update_docx_status(document, "godkänd")
    assert not has_review_watermark(document)


@pytest.mark.parametrize("status", ["för granskning", "godkänd"])
def test_update_xlsx_status_matches_baseline_byte_for_byte(tmp_path, status):
    new_copy = tmp_path / "new.xlsx"
    baseline_copy = tmp_path / "baseline.xlsx"
    _create_xlsx_template(new_copy)
    _create_xlsx_template(baseline_copy)

    update_xlsx_status(new_copy, status)
    _baseline_update_xlsx_status(baseline_copy, status)

    assert _read_sheet_xml(new_copy) == _read_sheet_xml(baseline_copy)


def test_update_document_status_rejects_unsupported_extension(tmp_path):
    unsupported = tmp_path / "fil.txt"
    unsupported.write_text("innehåll")
    with pytest.raises(ValueError):
        update_document_status(unsupported, "godkänd")


def test_update_document_status_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        update_document_status(tmp_path / "finns-inte.docx", "godkänd")


def test_process_directory_matches_baseline(tmp_path):
    new_dir = tmp_path / "new"
    baseline_dir = tmp_path / "baseline"
    new_dir.mkdir()
    baseline_dir.mkdir()

    _create_docx_template(new_dir / "a.docx")
    _create_xlsx_template(new_dir / "b.xlsx")
    _create_docx_template(baseline_dir / "a.docx")
    _create_xlsx_template(baseline_dir / "b.xlsx")

    new_processed = process_directory(new_dir, "för granskning")
    baseline_processed = _baseline_process_directory(baseline_dir, "för granskning")

    assert [p.name for p in new_processed] == [p.name for p in baseline_processed]
    assert _read_document_xml(new_dir / "a.docx") == _read_document_xml(baseline_dir / "a.docx")
    assert _read_sheet_xml(new_dir / "b.xlsx") == _read_sheet_xml(baseline_dir / "b.xlsx")
