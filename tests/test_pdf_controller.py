"""Tester för PDF-metadatapanelens kontroller och PDF-sökning (krav PDF-1)."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from app.controllers import pdf_controller
from backend.file_discovery import find_pdf_files
from dwg.pdf_sync import load_status_records, record_verified_pdf, status_for


def _make_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(595, 842)
    writer.write(path)
    return path


def test_find_pdf_files_is_recursive_and_skips_hidden_entries(tmp_path):
    root = tmp_path.resolve()
    expected = [
        _make_pdf(root / "a.pdf"),
        _make_pdf(root / "Ritningar" / "B.PDF"),
        _make_pdf(root / "Ritningar" / "c.pdf"),
    ]
    _make_pdf(root / ".datasamordning_backup" / "gammal.pdf")
    _make_pdf(root / "Ritningar" / ".plan.123.tmp.pdf")
    (root / "Ritningar" / "ritning.dwg").touch()

    assert find_pdf_files(root) == expected
    assert find_pdf_files(root, recursive=False) == expected[:1]
    assert find_pdf_files(expected[1]) == [expected[1]]


def test_read_watermark_states_reports_unreadable_files(tmp_path):
    good = _make_pdf(tmp_path / "bra.pdf")
    bad = tmp_path / "trasig.pdf"
    bad.write_bytes(b"inte en pdf")

    entries = pdf_controller.read_watermark_states([good, bad])

    assert entries[0] == pdf_controller.PdfEntry(good, None)
    assert entries[0].has_watermark is False
    assert entries[1].has_watermark is None and entries[1].error


def test_apply_watermarks_keeps_synced_dwg_pdf_synced(tmp_path):
    root = tmp_path.resolve()
    drawing = root / "plan.dwg"
    drawing.write_bytes(b"dwg")
    pdf = _make_pdf(root / "plan.pdf")
    record_verified_pdf(root, drawing, pdf)

    results = pdf_controller.apply_watermarks(root, {pdf: True})

    assert [(result.changed, result.error) for result in results] == [(True, None)]
    assert status_for(root, drawing, load_status_records(root))[0] == "synced"


def test_apply_watermarks_does_not_mark_stale_pdf_as_synced(tmp_path):
    root = tmp_path.resolve()
    drawing = root / "plan.dwg"
    drawing.write_bytes(b"dwg")
    pdf = _make_pdf(root / "plan.pdf")
    record_verified_pdf(root, drawing, pdf)
    drawing.write_bytes(b"dwg changed after plot")

    pdf_controller.apply_watermarks(root, {pdf: True})

    assert status_for(root, drawing, load_status_records(root))[0] == "stale"


def test_apply_watermarks_without_sibling_drawing_writes_no_sync_record(tmp_path):
    root = tmp_path.resolve()
    pdf = _make_pdf(root / "dokument.pdf")

    results = pdf_controller.apply_watermarks(root, {pdf: True})

    assert results[0].changed and results[0].error is None
    assert load_status_records(root) == {}
