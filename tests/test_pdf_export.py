from pathlib import Path

import pytest

import backend.pdf_export as pdf_export
from backend.pdf_export import PdfExportError, _office_export_script, plot_document


def _write_fake_pdf(source: Path, destination: Path) -> None:
    assert source.is_file()
    destination.write_bytes(b"%PDF-new")


def test_word_pdf_export_prefers_local_printer_before_opening_document(tmp_path):
    source = tmp_path / "ritning.docx"
    output = tmp_path / "ritning.pdf"

    script = _office_export_script(source, output)

    select_printer = script.index('application.ActivePrinter = "Microsoft Print to PDF"')
    open_document = script.index("application.Documents.Open")
    assert select_printer < open_document
    assert "If Err.Number <> 0 Then Err.Clear" in script[select_printer:open_document]


def test_excel_pdf_export_does_not_change_active_printer(tmp_path):
    source = tmp_path / "lista.xlsx"
    output = tmp_path / "lista.pdf"

    script = _office_export_script(source, output)

    assert "application.ActivePrinter" not in script
    assert "application.Workbooks.Open" in script


def test_pdf_is_copied_when_network_share_rejects_replace(tmp_path, monkeypatch):
    source = tmp_path / "ritning.docx"
    source.write_bytes(b"docx")
    output = source.with_suffix(".pdf")
    output.write_bytes(b"%PDF-old")

    def reject_replace(source_file, destination_file):
        assert source_file.is_file()
        assert destination_file == output
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(pdf_export.os, "replace", reject_replace)

    result = plot_document(source, exporter=_write_fake_pdf)

    assert result == output
    assert output.read_bytes() == b"%PDF-new"
    assert list(tmp_path.glob("*.pdf")) == [output]


def test_failed_network_pdf_copy_restores_previous_pdf(tmp_path, monkeypatch):
    source = tmp_path / "ritning.docx"
    source.write_bytes(b"docx")
    output = source.with_suffix(".pdf")
    previous_pdf = b"%PDF-previous"
    output.write_bytes(previous_pdf)

    def reject_replace(source_file, destination_file):
        assert source_file.is_file()
        assert destination_file == output
        raise PermissionError(5, "Access is denied")

    def fail_during_copy(source_file, destination_file, length=0):
        assert length == 0
        destination_file.write(source_file.read(5))
        raise OSError("network disconnected during copy")

    monkeypatch.setattr(pdf_export.os, "replace", reject_replace)
    monkeypatch.setattr(pdf_export.shutil, "copyfileobj", fail_during_copy)

    with pytest.raises(PdfExportError, match="PDF kunde inte publiceras"):
        plot_document(source, exporter=_write_fake_pdf)

    assert output.read_bytes() == previous_pdf
    assert list(tmp_path.glob("*.pdf")) == [output]


def test_failed_network_pdf_copy_without_previous_pdf_leaves_no_partial_output(tmp_path, monkeypatch):
    source = tmp_path / "ritning.docx"
    source.write_bytes(b"docx")
    output = source.with_suffix(".pdf")

    def reject_replace(source_file, destination_file):
        assert source_file.is_file()
        assert destination_file == output
        raise PermissionError(5, "Access is denied")

    def fail_during_copy(source_file, destination_file, length=0):
        assert length == 0
        destination_file.write(source_file.read(5))
        raise OSError("network disconnected during copy")

    monkeypatch.setattr(pdf_export.os, "replace", reject_replace)
    monkeypatch.setattr(pdf_export.shutil, "copyfileobj", fail_during_copy)

    with pytest.raises(PdfExportError, match="PDF kunde inte publiceras"):
        plot_document(source, exporter=_write_fake_pdf)

    assert not output.exists()
    assert list(tmp_path.glob("*.pdf")) == []
